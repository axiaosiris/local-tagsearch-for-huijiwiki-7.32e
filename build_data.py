#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_data.py —— 从 huiji 旧版 wiki 数据库构建检索器数据（data.js）

数据来源（默认 ../dotawiki-master/dotawiki-master/database/）：
  text_base.json    游戏 KV 基础数据（英雄/非英雄单位/物品/技能/翻译）
  mech.json         机制定义（目标/机制/次级/标记/叠加/魔法消耗/冷却时间/即时生效/属性标识）
  entry_base.json   词汇库（叠加方式等 10 条）
  version_base.json 版本更新内容（288 个版本，0.6 ~ 7.32e）
  version_list.json 版本号列表（366 个）
  name_base.json    曾用名（历史 197 / 原生 2975 / 衍生 548）
  ../huijidata/<分类>/*.json  技能源页面（7.32e，含效果/技能行为/独立机制等机制字段，用于补齐效果类标签）

输出：window.HUIJIDB = { builtAt, source, version, collections: {…} }
  collections.<名称> = {
    label, note,
    tags:   [{id, name, c1, c2}]          该集合的条件标签体系（id 全局唯一）
    entries:[{i, n, en?, … 集合特有字段, tags:[tagId…]}]
    scopes: [{key, label}]                匹配范围复选框（技能集合专用；其余为 []）
  }

标签语义说明（重要）：
  * 技能集合的标签由 7.32e 的两类旧版数据派生：
      ① 游戏 KV 字段（施法类型/目标/技能免疫/驱散/伤害类型/升级/类别/归属）；
      ② 技能源页面机制字段（包含效果/技能行为/独立机制/无敌/减益免疫）。
    二者同属旧版快照，效果类标签（沉默/锁闭/眩晕/缴械…）来自 ②。
  * 版本隔离：本检索器严禁混入现行网页 wiki（7.4x）的任何数据。旧版与现行是两个独立版本，
    混用会污染旧版快照。所有标签的语义仅以 7.32e 游戏 KV 为准。
  * 「驱散情况」映射（仅依据 7.32e 游戏 KV 令牌，不参考现行网页）：
      SPELL_DISPELLABLE_YES       -> 弱驱散
      SPELL_DISPELLABLE_YES_STRONG-> 强驱散
      SPELL_DISPELLABLE_NO        -> 无法驱散
  * 「技能免疫」沿用 7.32e 时代词汇（现行 wiki 已改称「减益免疫」）。
  * 「施法类型」中 AOE 令牌按 7.32e 惯例同时给出 点目标 + 范围指示。

用法：
  python build_data.py [数据库目录] [输出data.js路径] [huijidata目录]
  默认：数据库目录 = 脚本同级目录的 ../dotawiki-master/dotawiki-master/database
        输出 = 脚本同级目录的 data.js
        huijidata目录 = 脚本同级目录的 ../huijidata（含分类子目录，递归扫描）
"""

import json
import os
import re
import sys
import collections

# ---------------------------------------------------------------- 基础工具

def val(v):
    """text_base 的字段值都是 {"1": x} 包装，取内值"""
    if isinstance(v, dict):
        return v.get('1', '')
    return v


def num(v):
    """尽量把值转成数字（保留 int/float），失败/缺失返回空串"""
    x = val(v) if v is not None else None
    if x is None or x == '':
        return ''
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip()
    if not s:
        return ''
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return s


def txt(v):
    return str(val(v) if v is not None else '')


class TagPool:
    """全局标签池：按 (name, c1, c2) 去重分配 id"""

    def __init__(self):
        self.tags = []
        self._map = {}        # (name, c1, c2) -> id

    def get(self, name, c1='', c2='', src='kv'):
        key = (str(name), str(c1), str(c2))
        if key not in self._map:
            self._map[key] = len(self.tags) + 1
            self.tags.append({'id': len(self.tags) + 1, 'name': str(name),
                              'c1': str(c1), 'c2': str(c2), 'src': src})
        return self._map[key]

    def get_many(self, names, c1='', c2=''):
        return [self.get(n, c1, c2) for n in names]


# ---------------------------------------------------------------- KV 令牌 -> 中文标签

BEHAVIOR_MAP = {
    'DOTA_ABILITY_BEHAVIOR_PASSIVE':      [('被动', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_NO_TARGET':    [('无目标', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_UNIT_TARGET':  [('单位目标', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_POINT':        [('点目标', '施法类型', '')],
    # AOE：7.32e 惯例 = 点目标 + 范围指示（游戏 KV 派生，非现行网页对照）
    'DOTA_ABILITY_BEHAVIOR_AOE':          [('点目标', '施法类型', ''), ('范围指示', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_DIRECTIONAL':  [('矢量目标', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_CHANNELLED':   [('持续施法', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_TOGGLE':       [('开关/切换', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_IMMEDIATE':    [('立即施法', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_NONE':         [('无施法行为', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_IGNORE_BACKSWING': [('无视攻击后摇', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_AUTOCAST':     [('自动施法', '施法类型', '')],
    'DOTA_ABILITY_BEHAVIOR_ROOT_DISABLES': [('被缠绕时不可施放', '施法类型', '')],
}

TARGET_TEAM_MAP = {
    'DOTA_UNIT_TARGET_TEAM_ENEMY':   ('敌方', '目标', '势力'),
    'DOTA_UNIT_TARGET_TEAM_FRIENDLY': ('友方', '目标', '势力'),
    'DOTA_UNIT_TARGET_TEAM_BOTH':    ('敌我', '目标', '势力'),
    'DOTA_UNIT_TARGET_TEAM_CUSTOM':  ('自定义', '目标', '势力'),
}

TARGET_TYPE_MAP = {
    'DOTA_UNIT_TARGET_HERO':     ('英雄', '目标', '类型'),
    'DOTA_UNIT_TARGET_BASIC':    ('普通单位', '目标', '类型'),
    'DOTA_UNIT_TARGET_BUILDING': ('建筑', '目标', '类型'),
    'DOTA_UNIT_TARGET_TREE':     ('树', '目标', '类型'),
    'DOTA_UNIT_TARGET_COURIER':  ('信使', '目标', '类型'),
    'DOTA_UNIT_TARGET_CUSTOM':   ('自定义', '目标', '类型'),
    'DOTA_UNIT_TARGET_OTHER':    ('其他', '目标', '类型'),
}

# 驱散情况映射（依据 7.32e 游戏 KV 令牌；弱驱散/强驱散/无法驱散 三类）
DISPELL_MAP = {
    'SPELL_DISPELLABLE_YES':       ('弱驱散', '驱散情况', ''),
    'SPELL_DISPELLABLE_YES_STRONG': ('强驱散', '驱散情况', ''),
    'SPELL_DISPELLABLE_NO':        ('无法驱散', '驱散情况', ''),
}

# 7.32e 时代词汇（现行 wiki 已改称「减益免疫」）
IMMUNITY_MAP = {
    'SPELL_IMMUNITY_ENEMIES_YES':           ('敌军有视技能免疫', '技能免疫', ''),
    'SPELL_IMMUNITY_ENEMIES_NO':            ('无视技能免疫', '技能免疫', ''),
    'SPELL_IMMUNITY_ALLIES_YES':            ('友军有视技能免疫', '技能免疫', ''),
    'SPELL_IMMUNITY_ALLIES_NO':             ('友军无视技能免疫', '技能免疫', ''),
    'SPELL_IMMUNITY_ALLIES_YES_ENEMIES_NO': ('友军有视、敌军无视技能免疫', '技能免疫', ''),
}

DAMAGE_TYPE_MAP = {
    'DAMAGE_TYPE_MAGICAL':  ('魔法', '伤害类型', ''),
    'DAMAGE_TYPE_PHYSICAL': ('物理', '伤害类型', ''),
    'DAMAGE_TYPE_PURE':     ('纯粹', '伤害类型', ''),
    'DAMAGE_TYPE_NONE':     ('无类型', '伤害类型', ''),
}

ITEM_QUALITY_MAP = {
    'common': '普通', 'uncommon': '罕见', 'rare': '稀有',
    'epic': '史诗', 'secret': '隐秘', 'component': '配件',
    'consumable': '消耗品', 'artifact': '神器',
}

ITEM_SHOP_MAP = {
    'int': '智力', 'str': '力量', 'agi': '敏捷', 'damage': '输出',
    'consumable': '消耗品', 'regen_health': '生命恢复', 'armor': '护甲',
    'attack_speed': '攻击速度', 'teleport': '传送', 'hard_to_tag': '难以归类',
    'move_speed': '移动速度', 'regen_mana': '魔法恢复', 'health': '生命',
    'mana': '魔法', 'vision': '视野', 'evasion': '闪避', 'magic_block': '魔法格挡',
    'magic_essence': '魔法精华', ' Attributes': '属性',
}

# 技能详情中展示的 KV 字段（存在才输出）
SKILL_KV_FIELDS = [
    'ID', 'AbilityBehavior', 'AbilityType', 'AbilityUnitTargetTeam', 'AbilityUnitTargetType',
    'AbilityUnitDamageType', 'SpellImmunityType', 'SpellDispellableType', 'FightRecapLevel',
    'AbilityCastRange', 'AbilityCastPoint', 'AbilityCooldown', 'AbilityManaCost',
    'AbilityDuration', 'AbilityDamage', 'AbilityModifier', 'radius',
    'HasScepterUpgrade', 'HasShardUpgrade', 'AbilityCastAnimation',
]

HTML_TAG_RE = re.compile(r'<[^>]+>')
H_TEMPLATE_RE = re.compile(r'\{\{H\|([^|}]+)\}\}')
ANY_TEMPLATE_RE = re.compile(r'\{\{[^{}]*\}\}')


def clean_wiki_text(s):
    """去掉 wiki 模板/HTML，得到可读纯文本"""
    s = str(s or '')
    s = H_TEMPLATE_RE.sub(r'\1', s)
    s = ANY_TEMPLATE_RE.sub('', s)
    s = HTML_TAG_RE.sub('', s)
    return s.strip()


# ---------------------------------------------------------------- 技能源页面机制字段（7.32e）
# 技能源 JSON（huijidata/<分类>/<名称>.json）的机制字段用数字代码表示，代码→名称取自 mech.json。
# 这些数据与 KV 同属 7.32e 旧版快照，不涉及现行网页 wiki。
SRC_META_KEYS = {'名称', '代码', '驱散', '技能免疫', '状态抗性', '无敌', '标记',
                 '叠加', '生效从属', '生效目标', '自定义机制', '减益免疫'}

# 维度顺序（用于 scopes）
SRC_TAG_DIMS = ['包含效果', '技能行为', '独立机制', '无敌', '减益免疫', '状态抗性']


def _code(v):
    """从 {'代码': x} / {'1': {'代码': x}} 取代码，取不到返回 None"""
    if isinstance(v, dict):
        if '代码' in v:
            return v.get('代码')
        for sub in v.values():
            if isinstance(sub, dict) and '代码' in sub:
                return sub.get('代码')
    return None


def mech_label(mech, top, group, code):
    """mech.json 中按 顶层/分组/代码 取中文名"""
    g = mech.get(top, {})
    g = g.get(group) if isinstance(g, dict) else None
    if not isinstance(g, dict):
        return ''
    e = g.get(str(code))
    if isinstance(e, str):
        return e.strip()
    if isinstance(e, dict):
        return str(e.get('名称', '')).strip()
    return ''


def load_skill_sources(huijidata_dir):
    """读 huijidata 下（含分类子目录）所有 分类=技能源 的条目 -> {英文代码: dict}"""
    src = {}
    if not huijidata_dir or not os.path.isdir(huijidata_dir):
        return src
    for root, dirs, files in os.walk(huijidata_dir):
        for fn in sorted(files):
            if not fn.endswith('.json') or fn.startswith('_'):
                continue
            try:
                with open(os.path.join(root, fn), 'r', encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            if not isinstance(d, dict) or d.get('分类') != '技能源':
                continue
            code = str(d.get('代码') or '').strip()
            if code:
                src[code] = d
    return src


def source_tags(d, mech):
    """从一条技能源 JSON 提取 (标签名, 维度) 列表。"""
    out = []

    def put(name, c1):
        name = str(name or '').strip()
        if name and not name.startswith('#'):
            out.append((name, c1))

    # 1) 包含效果：效果组下的具名子效果（沉默/锁闭/眩晕/缴械/破坏…）
    eff = d.get('效果')
    if isinstance(eff, dict):
        for gv in eff.values():
            if not isinstance(gv, dict):
                continue
            for k, sv in gv.items():
                if k in SRC_META_KEYS or not isinstance(sv, dict):
                    continue
                put(sv.get('名称'), '包含效果')
            # 效果级机制（次级词典）：减益免疫 / 无敌 / 状态抗性
            nm = mech_label(mech, '次级', '减益免疫', _code(gv.get('减益免疫')))
            if nm:
                put(nm, '减益免疫')
            nm = mech_label(mech, '次级', '无敌', _code(gv.get('无敌')))
            if nm:
                put(nm, '无敌')
            nm = mech_label(mech, '次级', '状态抗性', _code(gv.get('状态抗性')))
            if nm:
                put(nm, '状态抗性')

    # 2) 技能行为：顶层机制字段（代码 -> 名称 -> 语义标签）
    for field in ('技能抵挡', '技能反弹', '技能共享'):
        nm = mech_label(mech, '机制', field, _code(d.get(field)))
        if nm in ('会触发', '主次均触发') or nm.startswith('主完全抵挡') or nm.startswith('主触发'):
            put(field, '技能行为')
    nm = mech_label(mech, '机制', '技能窃取', _code(d.get('技能窃取')))
    if nm in ('可以被窃取', '部分情况能被窃取', '不能主动窃取，而是被动获得'):
        put('可被窃取' if nm == '可以被窃取' else nm, '技能行为')
    nm = mech_label(mech, '机制', '幻象', _code(d.get('幻象')))
    if nm in ('会被幻象继承', '部分效果被幻象继承'):
        put('幻象继承' if nm == '会被幻象继承' else '部分幻象继承', '技能行为')
    nm = mech_label(mech, '机制', '破坏', _code(d.get('破坏')))
    if nm in ('会被破坏禁用', '部分效果被破坏禁用'):
        put('破坏禁用' if nm == '会被破坏禁用' else '部分破坏禁用', '技能行为')
    nm = mech_label(mech, '机制', '持续施法', _code(d.get('持续施法')))
    if nm in ('会打断持续施法', '部分情况会打断持续施法'):
        put('打断持续施法' if nm == '会打断持续施法' else '部分情况打断持续施法', '技能行为')
    nm = mech_label(mech, '机制', '躲避', _code(d.get('躲避')))
    if nm in ('弹道可以被躲避', '部分弹道可以被躲避'):
        put('弹道可躲避' if nm == '弹道可以被躲避' else '部分弹道可躲避', '技能行为')
    nm = mech_label(mech, '机制', '缠绕', _code(d.get('缠绕')))
    if nm in ('被缠绕或束缚时不可以释放', '部分情况下被缠绕或束缚时不可释放'):
        put('被缠绕时不可施放' if nm.startswith('被缠绕') else '部分情况被缠绕时不可施放', '技能行为')
    if mech_label(mech, '机制', '即时攻击', _code(d.get('即时攻击'))):
        put('即时攻击', '技能行为')
    nm = mech_label(mech, '机制', '视野', _code(d.get('视野')))
    if nm:
        put(nm, '技能行为')
    nm = mech_label(mech, '机制', '真实视域', _code(d.get('真实视域')))
    if nm:
        put(nm, '技能行为')

    # 3) 独立机制：机制名
    im = d.get('独立机制')
    if isinstance(im, dict):
        for v in im.values():
            if isinstance(v, dict):
                put(v.get('机制名'), '独立机制')

    # 4) 无敌 / 减益免疫
    nm = mech_label(mech, '机制', '无敌', _code(d.get('无敌')))
    if nm:
        put(nm, '无敌')
    nm = mech_label(mech, '次级', '减益免疫', _code(d.get('减益免疫')))
    if nm:
        put(nm, '减益免疫')

    # 5) 升级（源页面）
    up = d.get('升级')
    if isinstance(up, dict):
        if up.get('神杖'):
            put('神杖升级', '升级')
        if up.get('魔晶'):
            put('魔晶升级', '升级')

    return out


# ---------------------------------------------------------------- 版本隔离声明
# 本检索器仅基于 huiji 旧版 wiki 数据库（7.32e 快照）构建：
#   ① 游戏 KV 字段派生（施法类型/目标/技能免疫/驱散/伤害类型/升级/类别/归属）
#   ② 技能源页面机制字段（包含效果/技能行为/独立机制/无敌/减益免疫）
# 二者同属旧版快照。严禁引入现行网页 wiki（7.4x）的任何数据，混用会污染旧版快照。

def build(db_dir, huijidata_dir=None):
    with open(os.path.join(db_dir, 'text_base.json'), 'r', encoding='utf-8') as f:
        tb = json.load(f)
    with open(os.path.join(db_dir, 'mech.json'), 'r', encoding='utf-8') as f:
        mech = json.load(f)
    with open(os.path.join(db_dir, 'entry_base.json'), 'r', encoding='utf-8') as f:
        entry_base = json.load(f)
    with open(os.path.join(db_dir, 'version_base.json'), 'r', encoding='utf-8') as f:
        version_base = json.load(f)
    with open(os.path.join(db_dir, 'name_base.json'), 'r', encoding='utf-8') as f:
        name_base = json.load(f)

    sources = load_skill_sources(huijidata_dir)

    tr = tb.get('翻译', {})

    def cn_of(key):
        v = tr.get(key)
        if isinstance(v, dict):
            return (v.get('中文') or '').strip()
        return ''

    pool = TagPool()
    collections_out = {}

    # ================================================================ 技能
    heroes_en = set(tb['英雄'].keys())
    items_en = set(tb['物品'].keys())
    hero_cn = {en: cn_of('npc_dota_hero_' + en) or en for en in heroes_en}
    item_cn = {en: cn_of('DOTA_Tooltip_Ability_item_' + en) or en for en in items_en}

    def owner_of(name):
        """派生技能归属：返回 (归属中文名, 类别标签列表)"""
        cats = []
        if name.startswith('special_bonus_'):
            cats.append('天赋技能')
            for h in heroes_en:
                if ('_' + h + '_') in ('_' + name) or name.endswith('_' + h):
                    return hero_cn[h], cats
            return '天赋', cats
        if name.startswith('item_') or name in items_en:
            cats.append('物品技能')
            for it in items_en:
                if name == it or name.startswith(it + '_') or name == 'item_' + it:
                    return item_cn[it], cats
            return '物品', cats
        if name.startswith('generic_') or name.startswith('ability_'):
            return '通用', cats
        for h in heroes_en:
            if name.startswith(h + '_'):
                return hero_cn[h], cats
        for it in items_en:
            if name.startswith(it + '_'):
                return item_cn[it], cats
        return name.split('_')[0], cats

    skill_entries = []
    for en, kv in tb['技能'].items():
        behavior = txt(kv.get('AbilityBehavior'))
        team = txt(kv.get('AbilityUnitTargetTeam'))
        ttype = txt(kv.get('AbilityUnitTargetType'))
        dispel = txt(kv.get('SpellDispellableType'))
        immun = txt(kv.get('SpellImmunityType'))
        dtype = txt(kv.get('AbilityUnitDamageType'))

        tags = []
        for n, c1, c2 in BEHAVIOR_MAP.get(behavior, []):
            tags.append(pool.get(n, c1, c2))
        if team in TARGET_TEAM_MAP:
            tags.append(pool.get(*TARGET_TEAM_MAP[team]))
        if ttype in TARGET_TYPE_MAP:
            tags.append(pool.get(*TARGET_TYPE_MAP[ttype]))
        if dispel in DISPELL_MAP:
            tags.append(pool.get(*DISPELL_MAP[dispel]))
        if immun in IMMUNITY_MAP:
            tags.append(pool.get(*IMMUNITY_MAP[immun]))
        if dtype in DAMAGE_TYPE_MAP:
            tags.append(pool.get(*DAMAGE_TYPE_MAP[dtype]))
        if num(kv.get('HasScepterUpgrade')) == 1:
            tags.append(pool.get('神杖升级', '升级', ''))
        if num(kv.get('HasShardUpgrade')) == 1:
            tags.append(pool.get('魔晶升级', '升级', ''))
        if 'DOTA_ABILITY_BEHAVIOR_HIDDEN' in behavior:
            tags.append(pool.get('隐藏技能', '类别', ''))
        if 'DOTA_ABILITY_BEHAVIOR_NOT_LEARNABLE' in behavior:
            tags.append(pool.get('非学习技能', '类别', ''))

        owner, cats = owner_of(en)
        for c in cats:
            tags.append(pool.get(c, '类别', ''))
        if owner:
            tags.append(pool.get(owner, '归属', ''))

        # —— 技能源页面机制字段（7.32e）：包含效果/技能行为/独立机制/无敌/减益免疫/升级 ——
        src = sources.get(en)
        if src:
            for nm, c1 in source_tags(src, mech):
                tags.append(pool.get(nm, c1, ''))

        detail = {}
        for fk in SKILL_KV_FIELDS:
            if fk in kv:
                v = num(kv[fk])
                if v != '':
                    detail[fk] = v
        lore = cn_of('DOTA_Tooltip_ability_' + en + '_Lore')
        if lore:
            detail['lore'] = lore

        skill_entries.append({
            'i': len(skill_entries) + 1,
            'n': cn_of('DOTA_Tooltip_ability_' + en) or en,
            'en': en,
            'gid': num(kv.get('ID')),
            'own': owner,
            'tags': sorted(set(tags)),
            'kv': detail,
        })
    skill_entries.sort(key=lambda e: (e['n'], e['en']))
    for k, e in enumerate(skill_entries):
        e['i'] = k + 1

    src_matched = sum(1 for e in skill_entries if e['en'] in sources)
    collections_out['技能'] = {
        'label': '技能',
        'note': 'text_base[技能] 共 %d 条。标签来源：① <b>游戏 KV 派生</b>（施法类型/目标/技能免疫/驱散/伤害类型/升级/类别/归属）；'
                '② <b>技能源页面机制字段</b>（包含效果/技能行为/独立机制/无敌/减益免疫/状态抗性，已匹配 %d 条）。'
                '数据均为 huiji 旧版 7.32e 快照，<b>未引入任何现行网页 wiki 内容</b>。'
                % (len(skill_entries), src_matched),
        'tags': None,   # 占位，最后统一回填
        'entries': skill_entries,
        'scopes': [
            {'key': 'cast', 'label': '施法类型', 'c1': '施法类型'},
            {'key': 'tgt', 'label': '目标', 'c1': '目标'},
            {'key': 'imm', 'label': '技能免疫', 'c1': '技能免疫'},
            {'key': 'disp', 'label': '驱散情况', 'c1': '驱散情况'},
            {'key': 'dmg', 'label': '伤害类型', 'c1': '伤害类型'},
            {'key': 'up', 'label': '升级', 'c1': '升级'},
            {'key': 'cat', 'label': '类别', 'c1': '类别'},
            {'key': 'own', 'label': '归属', 'c1': '归属'},
            {'key': 'eff', 'label': '包含效果', 'c1': '包含效果'},
            {'key': 'beh', 'label': '技能行为', 'c1': '技能行为'},
            {'key': 'ind', 'label': '独立机制', 'c1': '独立机制'},
            {'key': 'invul', 'label': '无敌', 'c1': '无敌'},
            {'key': 'dbimm', 'label': '减益免疫', 'c1': '减益免疫'},
            {'key': 'stress', 'label': '状态抗性', 'c1': '状态抗性'},
        ],
        'cols': [
            {'k': 'n', 't': '技能'}, {'k': 'en', 't': '英文名'}, {'k': 'gid', 't': '游戏ID'},
            {'k': '_hits', 't': '命中标签'}, {'k': '_cnt', 't': 'tag数'}, {'k': '_d', 't': ''},
        ],
    }

    # ================================================================ 机制
    mech_entries = []
    for top, groups in mech.items():
        if top == '目标':
            # 势力
            for code, e in groups.get('势力', {}).items():
                mech_entries.append({
                    'i': 0, 'n': e.get('名称', code), 'grp': '目标 / 势力',
                    'brief': '颜色 ' + str(e.get('颜色', '')),
                    'tags': pool.get_many(['目标', '势力'], '机制分组', top),
                })
            for code, e in groups.get('分类', {}).items():
                subs = [v for k, v in e.items() if k != '名称']
                mech_entries.append({
                    'i': 0, 'n': e.get('名称', code), 'grp': '目标 / 分类',
                    'brief': '、'.join(str(s) for s in subs),
                    'tags': pool.get_many(['目标', '分类'], '机制分组', top),
                })
            continue
        if top == '属性标识':
            for name, e in groups.items():
                mech_entries.append({
                    'i': 0, 'n': name, 'grp': '属性标识',
                    'brief': e.get('描述', ''),
                    'tags': pool.get_many(['属性标识'], '机制分组', top),
                })
            continue
        for gname, items in groups.items():
            # 扁平结构：subgroup 本身即条目（魔法消耗/冷却时间/即时生效）
            if isinstance(items, dict) and '名称' in items:
                brief = items.get('简述', '') or items.get('后缀', '')
                mech_entries.append({
                    'i': 0, 'n': items['名称'], 'grp': top,
                    'brief': brief,
                    'tags': pool.get_many([top], '机制分组', top),
                })
                continue
            for code, e in items.items():
                if isinstance(e, dict) and '名称' in e:
                    mech_entries.append({
                        'i': 0, 'n': e['名称'], 'grp': '%s / %s' % (top, gname),
                        'brief': e.get('简述', ''),
                        'tags': pool.get_many([top, gname], '机制分组', top),
                    })
                elif isinstance(e, str) and e.strip():
                    mech_entries.append({
                        'i': 0, 'n': e, 'grp': '%s / %s' % (top, gname),
                        'brief': '',
                        'tags': pool.get_many([top, gname], '机制分组', top),
                    })
    mech_entries.sort(key=lambda e: (e['grp'], e['n']))
    for k, e in enumerate(mech_entries):
        e['i'] = k + 1
    collections_out['机制'] = {
        'label': '机制',
        'note': 'mech.json 机制定义 %d 条（目标/机制/次级/标记/叠加/魔法消耗/冷却时间/即时生效/属性标识），简述为 7.32e 时代原文' % len(mech_entries),
        'tags': None,
        'entries': mech_entries,
        'scopes': [],
        'cols': [{'k': 'n', 't': '名称'}, {'k': 'grp', 't': '分组'}, {'k': 'brief', 't': '简述'}],
    }

    # ================================================================ 曾用名
    name_entries = []
    kind_cn = {'历史': '历史曾用名', '原生': '原生页面', '衍生': '衍生页面'}
    for kind in ('历史', '原生', '衍生'):
        for e in name_base.get(kind, []):
            name_entries.append({
                'i': 0, 'n': e.get('名称', ''), 'page': e.get('页面名', ''),
                'kind': kind_cn.get(kind, kind),
                'tags': [pool.get(kind_cn.get(kind, kind), '类别', '')],
            })
    name_entries.sort(key=lambda e: (e['kind'], e['n']))
    for k, e in enumerate(name_entries):
        e['i'] = k + 1
    collections_out['曾用名'] = {
        'label': '曾用名',
        'note': 'name_base.json：历史 %d / 原生 %d / 衍生 %d' % (
            len(name_base.get('历史', [])), len(name_base.get('原生', [])), len(name_base.get('衍生', []))),
        'tags': None,
        'entries': name_entries,
        'scopes': [],
        'cols': [{'k': 'n', 't': '名称'}, {'k': 'page', 't': '页面名'}, {'k': 'kind', 't': '类别'}],
    }

    # ================================================================ 版本更新
    ver_entries = []
    for vname, v in version_base.items():
        ents = []
        tagset = set()
        tagset.add(pool.get(txt(v.get('分类')) or '版本更新', '分类', ''))
        date = txt(v.get('更新日期'))
        ym = re.match(r'(\d{4})', date)
        if ym:
            tagset.add(pool.get(ym.group(1) + '年', '更新年份', ''))
        for grp in ('英雄', '物品', '建筑', '兵线', '通用', '开头'):
            block = v.get(grp)
            if not isinstance(block, dict):
                continue
            for gkey, rows in block.items():
                if not isinstance(rows, dict):
                    continue
                for idx, row in rows.items():
                    if not isinstance(row, list):
                        continue
                    target = row[0] if row and isinstance(row[0], str) else ''
                    for cell in row[1:]:
                        if not isinstance(cell, dict):
                            continue
                        text = clean_wiki_text(cell.get('文字', ''))
                        if not text:
                            continue
                        tgts = cell.get('目标') or []
                        if not target and tgts:
                            target = str(tgts[0])
                        tags = [str(t) for t in (cell.get('标签') or [])]
                        ents.append({'g': grp, 't': target or gkey, 'x': text, 'tags': tags})
                        for t in tgts:
                            if t:
                                tagset.add(pool.get(str(t), '更新实体', ''))
        counts = collections.Counter(e['g'] for e in ents)
        ver_entries.append({
            'i': 0, 'n': vname, 'date': date, 'cat': txt(v.get('分类')) or '版本更新',
            'link': txt(v.get('官网链接')),
            'counts': dict(counts),
            'ents': ents,
            'tags': sorted(tagset),
        })
    ver_entries.sort(key=lambda e: [int(x) if x.isdigit() else 0 for x in re.findall(r'\d+', e['n'])] or [0])
    for k, e in enumerate(ver_entries):
        e['i'] = k + 1
    collections_out['版本更新'] = {
        'label': '版本更新',
        'note': 'version_base.json：%d 个版本（0.6 ~ 7.32e）的更新条目；「更新实体」标签 = 该版本更新条目中出现的英雄/物品/单位名，可用「或」条件查某实体历次改动' % len(ver_entries),
        'tags': None,
        'entries': ver_entries,
        'scopes': [],
        'cols': [{'k': 'n', 't': '版本'}, {'k': 'date', 't': '更新日期'}, {'k': '_cnt', 't': '更新条目'}, {'k': '_hits', 't': '命中实体'}, {'k': '_d', 't': ''}],
    }

    # ================================================================ 词汇注释
    entry_entries = []
    for name, e in entry_base.items():
        entry_entries.append({
            'i': 0, 'n': name, 'link': e.get('链接', ''),
            'text': e.get('文字', ''), 'note': e.get('注释', ''),
            'dnote': e.get('伤害注释', ''),
            'tags': [pool.get('叠加方式', '词汇分组', '')],
        })
    entry_entries.sort(key=lambda e: e['n'])
    for k, e in enumerate(entry_entries):
        e['i'] = k + 1
    collections_out['词汇注释'] = {
        'label': '词汇注释',
        'note': 'entry_base.json 词汇库 %d 条（持续性技能叠加方式）' % len(entry_entries),
        'tags': None,
        'entries': entry_entries,
        'scopes': [],
        'cols': [{'k': 'n', 't': '词汇'}, {'k': 'link', 't': '链接'}, {'k': 'text', 't': '文字'}],
    }

    # ================================================================ 英雄
    hero_entries = []
    for en, kv in tb['英雄'].items():
        attr = txt(kv.get('AttributePrimary'))
        cap = txt(kv.get('AttackCapabilities'))
        tags = []
        if attr:
            tags.append(pool.get(attr, '主属性', ''))
        if cap:
            tags.append(pool.get(cap, '攻击类型', ''))
        hero_entries.append({
            'i': 0, 'n': hero_cn[en], 'en': en,
            'attr': attr, 'cap': cap,
            'stats': {
                '力量': num(kv.get('AttributeBaseStrength')), '力量成长': num(kv.get('AttributeStrengthGain')),
                '敏捷': num(kv.get('AttributeBaseAgility')), '敏捷成长': num(kv.get('AttributeAgilityGain')),
                '智力': num(kv.get('AttributeBaseIntelligence')), '智力成长': num(kv.get('AttributeIntelligenceGain')),
                '生命': num(kv.get('StatusHealth')), '魔法': num(kv.get('StatusMana')),
                '攻击': '%s - %s' % (num(kv.get('AttackDamageMin')), num(kv.get('AttackDamageMax'))),
                '护甲': num(kv.get('ArmorPhysical')), '移速': num(kv.get('MovementSpeed')),
                '攻击范围': num(kv.get('AttackRange')), '攻击间隔': num(kv.get('AttackRate')),
            },
            'tags': tags,
        })
    hero_entries.sort(key=lambda e: e['n'])
    for k, e in enumerate(hero_entries):
        e['i'] = k + 1
    collections_out['英雄'] = {
        'label': '英雄',
        'note': 'text_base[英雄] %d 个；stats 为 7.32e 基础值（未含成长后等级值）' % len(hero_entries),
        'tags': None,
        'entries': hero_entries,
        'scopes': [],
        'cols': [{'k': 'n', 't': '英雄'}, {'k': 'en', 't': '英文名'}, {'k': 'attr', 't': '主属性'}, {'k': 'cap', 't': '攻击类型'}, {'k': '_d', 't': ''}],
    }

    # ================================================================ 物品
    item_entries = []
    for en, kv in tb['物品'].items():
        quality = txt(kv.get('ItemQuality'))
        shop = txt(kv.get('ItemShopTags'))
        tags = []
        if quality in ITEM_QUALITY_MAP:
            tags.append(pool.get(ITEM_QUALITY_MAP[quality], '品质', ''))
        elif quality:
            tags.append(pool.get(quality, '品质', ''))
        for s in str(shop).split():
            if s in ITEM_SHOP_MAP:
                tags.append(pool.get(ITEM_SHOP_MAP[s], '商店标签', ''))
        item_entries.append({
            'i': 0, 'n': item_cn[en], 'en': en,
            'cost': num(kv.get('ItemCost')),
            'quality': ITEM_QUALITY_MAP.get(quality, quality),
            'shop': shop, 'tags': tags,
        })
    item_entries.sort(key=lambda e: e['n'])
    for k, e in enumerate(item_entries):
        e['i'] = k + 1
    collections_out['物品'] = {
        'label': '物品',
        'note': 'text_base[物品] %d 个（中文名覆盖 %d）' % (
            len(item_entries), sum(1 for e in item_entries if e['n'] != e['en'])),
        'tags': None,
        'entries': item_entries,
        'scopes': [],
        'cols': [{'k': 'n', 't': '物品'}, {'k': 'en', 't': '英文名'}, {'k': 'cost', 't': '价格'}, {'k': 'quality', 't': '品质'}, {'k': '_d', 't': ''}],
    }

    # ================================================================ 非英雄单位
    unit_entries = []
    for en, kv in tb['非英雄单位'].items():
        rel = txt(kv.get('UnitRelationshipClass'))
        is_neu = num(kv.get('IsNeutralUnitType')) == 1
        is_sum = num(kv.get('IsSummoned')) == 1
        is_hero = num(kv.get('ConsideredHero')) == 1
        cats = []
        if rel and rel != '默认':
            cats.append(rel)
        if is_neu:
            cats.append('中立生物')
        if is_sum:
            cats.append('召唤单位')
        if is_hero:
            cats.append('英雄级单位')
        if not cats:
            cats.append('其他单位')
        tags = [pool.get(c, '单位分类', '') for c in cats]
        unit_entries.append({
            'i': 0, 'n': cn_of('npc_dota_' + en) or en, 'en': en,
            'cats': cats,
            'stats': {
                '等级': num(kv.get('Level')),
                '生命': num(kv.get('StatusHealth')),
                '攻击': '%s - %s' % (num(kv.get('AttackDamageMin')), num(kv.get('AttackDamageMax'))),
                '护甲': num(kv.get('ArmorPhysical')),
                '移速': num(kv.get('MovementSpeed')),
                '魔抗': num(kv.get('MagicalResistance')),
                '金钱': '%s - %s' % (num(kv.get('BountyGoldMin')), num(kv.get('BountyGoldMax'))),
                '经验': num(kv.get('BountyXP')),
            },
            'tags': tags,
        })
    unit_entries.sort(key=lambda e: e['n'])
    for k, e in enumerate(unit_entries):
        e['i'] = k + 1
    collections_out['非英雄单位'] = {
        'label': '非英雄单位',
        'note': 'text_base[非英雄单位] %d 个' % len(unit_entries),
        'tags': None,
        'entries': unit_entries,
        'scopes': [],
        'cols': [{'k': 'n', 't': '单位'}, {'k': 'en', 't': '英文名'}, {'k': 'cats', 't': '分类'}, {'k': '_d', 't': ''}],
    }

    # ---------------------------------------------------------------- 回填 tags + 汇总
    for cname, c in collections_out.items():
        used = set()
        for e in c['entries']:
            used.update(e.get('tags') or [])
        c['tags'] = [t for t in pool.tags if t['id'] in used]

    db = {
        'builtAt': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'huiji/dotawiki-master/dotawiki-master/database',
        'version': '7.32e',
        'collections': collections_out,
    }
    return db


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    db_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        here, '..', 'dotawiki-master', 'dotawiki-master', 'database')
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, 'data.js')
    huijidata_dir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(here, '..', 'huijidata')
    db = build(os.path.abspath(db_dir), os.path.abspath(huijidata_dir))
    txt = 'window.HUIJIDB=' + json.dumps(db, ensure_ascii=False, separators=(',', ':')) + ';'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(txt)
    total = sum(len(c['entries']) for c in db['collections'].values())
    ntags = len({t['id'] for c in db['collections'].values() for t in c['tags']})
    print('构建完成 -> %s' % out)
    print('  集合 %d 个 / 条目 %d 条 / 标签 %d 个 / 文件 %.1f KB' % (
        len(db['collections']), total, ntags, len(txt.encode('utf-8')) / 1024))
    for k, c in db['collections'].items():
        print('    %-8s %5d 条  tags %4d' % (k, len(c['entries']), len(c['tags'])))
    print('  版本隔离：标签 = 7.32e KV 派生 + 技能源页面机制字段，未引入任何现行网页 wiki 数据')


if __name__ == '__main__':
    main()
