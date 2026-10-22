/* search-core.js —— huiji 旧版 wiki 数据库检索核心（浏览器 / Node 通用，无依赖）
 *
 * 数据模型（window.HUIJIDB，由 build_data.py 生成）：
 *   collections: {
 *     <名称>: {
 *       label, note,
 *       tags:   [{id, name, c1, c2}]     条件标签体系（id 全局唯一）
 *       entries:[{i, n, en?, … , tags:[tagId…]}]
 *       scopes: [{key, label, c1}]       匹配范围（技能集合用；其余为 []）
 *       cols:   [{k, t}]                 结果表列定义
 *     }
 *   }
 *
 * 条件模型（与既有 tag 检索器一致）：
 *   and: [tagName…] 必须全部包含
 *   or : [tagName…] 至少包含其一
 *   not: [tagName…] 全部不得包含
 *   name: 名称包含（子串；fuzzy 关闭时为全等）
 *   fuzzy: tag 名称为子串匹配（如「防御塔」→ 友方/敌方防御塔）
 *   scope: {scopeKey: bool} 仅技能集合生效，按标签 c1 过滤
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.HuijiSearch = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {

  var MAXC = 5;   // 每类条件上限

  /* 每个集合中参与「名称包含」匹配的字段 */
  var NAME_FIELDS = {
    '技能': ['n', 'en'],
    '机制': ['n'],
    '曾用名': ['n', 'page'],
    '版本更新': ['n'],
    '词汇注释': ['n'],
    '英雄': ['n', 'en'],
    '物品': ['n', 'en'],
    '非英雄单位': ['n', 'en']
  };

  function nameFieldsOf(cname) {
    return NAME_FIELDS[cname] || ['n', 'en'];
  }

  /* ---------- 索引 ---------- */

  function createIndex(db) {
    var cols = {};
    Object.keys(db.collections || {}).forEach(function (cname) {
      var c = db.collections[cname];
      var id2tag = {};
      var name2ids = {};
      (c.tags || []).forEach(function (t) {
        id2tag[t.id] = t;
        var arr = name2ids[t.name];
        if (!arr) { arr = []; name2ids[t.name] = arr; }
        if (arr.indexOf(t.id) < 0) arr.push(t.id);
      });
      var names = Object.keys(name2ids).sort(function (a, b) {
        return String(a).localeCompare(String(b), 'zh-Hans-CN');
      });
      cols[cname] = {
        name: cname, label: c.label, note: c.note, cols: c.cols || [],
        tags: c.tags || [], scopes: c.scopes || [],
        id2tag: id2tag, name2ids: name2ids, names: names,
        entries: c.entries || [], nameFields: nameFieldsOf(cname),
        c1s: (c.scopes || []).map(function (s) { return s.c1; })
      };
    });
    return { db: db, cols: cols, names: Object.keys(cols) };
  }

  /* ---------- 名称解析 ---------- */

  /** 输入 -> tag id 集合：精确优先；fuzzy 时按子串展开全部同名/含该词的 tag */
  function resolveName(cidx, raw, fuzzy) {
    var n = String(raw || '').trim();
    if (!n) return null;
    var exactIds = cidx.name2ids[n];
    if (exactIds && !fuzzy) {
      return { input: n, names: [n], ids: exactIds.slice(), exact: true };
    }
    var hits = cidx.names.filter(function (x) { return x.indexOf(n) >= 0; });
    if (!hits.length) {
      if (exactIds) return { input: n, names: [n], ids: exactIds.slice(), exact: true };
      return { input: n, names: [], ids: null, exact: false };
    }
    var ids = [];
    hits.forEach(function (x) {
      cidx.name2ids[x].forEach(function (id) { if (ids.indexOf(id) < 0) ids.push(id); });
    });
    return { input: n, names: hits, ids: ids, exact: (hits.length === 1 && hits[0] === n) };
  }

  /* ---------- 匹配 ---------- */

  function normalizeScope(cidx, scope) {
    var out = null;
    if (cidx.scopes && cidx.scopes.length) {
      out = {};
      cidx.scopes.forEach(function (s) { out[s.key] = true; });
      if (scope) cidx.scopes.forEach(function (s) {
        if (scope[s.key] === false) out[s.key] = false;
      });
    }
    return out;   // null = 不过滤
  }

  /** 按匹配范围收集某条目的 tag id 集合 */
  function tagSet(cidx, e, scope) {
    var sc = normalizeScope(cidx, scope);
    var s = {};
    (e.tags || []).forEach(function (id) {
      if (!sc) { s[id] = 1; return; }
      var t = cidx.id2tag[id];
      if (!t) return;
      var c1 = t.c1 || '';
      for (var i = 0; i < cidx.scopes.length; i++) {
        if (cidx.scopes[i].c1 === c1) {
          if (sc[cidx.scopes[i].key]) s[id] = 1;
          return;
        }
      }
    });
    return s;
  }

  /** 名称包含：始终按子串匹配（不区分大小写）；fuzzy 只作用于标签名解析 */
  function nameHit(cidx, e, name) {
    if (!name) return true;
    var q = String(name).toLowerCase();
    var fields = cidx.nameFields;
    for (var i = 0; i < fields.length; i++) {
      var v = e[fields[i]];
      if (v == null) continue;
      if (String(v).toLowerCase().indexOf(q) >= 0) return true;
    }
    return false;
  }

  function idsOf(r) { return (r && r.ids) || null; }
  function hasAny(set, ids) {
    for (var i = 0; i < ids.length; i++) if (set[ids[i]]) return true;
    return false;
  }

  /* ---------- 检索 ---------- */

  function search(cidx, query) {
    query = query || {};
    var fuzzy = !!query.fuzzy;
    var nameQ = String(query.name || '').trim();
    var scope = query.scope || null;

    var rAnd = (query.and || []).map(function (s) { return resolveName(cidx, s, fuzzy); }).filter(Boolean);
    var rOr = (query.or || []).map(function (s) { return resolveName(cidx, s, fuzzy); }).filter(Boolean);
    var rNot = (query.not || []).map(function (s) { return resolveName(cidx, s, fuzzy); }).filter(Boolean);

    var unknown = [];
    [rAnd, rOr, rNot].forEach(function (list) {
      list.forEach(function (r) { if (!r.ids) unknown.push(r.input); });
    });

    var hits = [];
    cidx.entries.forEach(function (e) {
      if (nameQ && !nameHit(cidx, e, nameQ)) return;
      var set = tagSet(cidx, e, scope);
      var i;
      for (i = 0; i < rAnd.length; i++) {
        var ia = idsOf(rAnd[i]);
        if (ia && !hasAny(set, ia)) return;
      }
      if (rOr.length) {
        var ok = false;
        for (i = 0; i < rOr.length; i++) {
          var io = idsOf(rOr[i]);
          if (io && hasAny(set, io)) { ok = true; break; }
        }
        if (!ok) return;
      }
      for (i = 0; i < rNot.length; i++) {
        var ino = idsOf(rNot[i]);
        if (ino && hasAny(set, ino)) return;
      }
      /* 命中详情：命中的条件 -> 其展开的 tag 名（含所在维度 c1） */
      var detail = [];
      var pushDetail = function (r, group) {
        if (!r.ids) return;
        var names = [];
        r.ids.forEach(function (id) {
          var t = cidx.id2tag[id];
          if (t && set[id]) names.push(t.name + (t.c1 ? '@' + t.c1 : ''));
        });
        if (names.length) detail.push({ group: group, input: r.input, names: names });
      };
      rAnd.forEach(function (r) { pushDetail(r, 'and'); });
      rOr.forEach(function (r) { pushDetail(r, 'or'); });

      hits.push({ entry: e, tagCount: Object.keys(set).length, detail: detail });
    });

    return {
      hits: hits,
      unknown: unknown,
      resolutions: { and: rAnd, or: rOr, not: rNot },
      scanned: cidx.entries.length
    };
  }

  /** 某条目的全部标签（按 c1 分组），供详情面板 */
  function describe(cidx, e) {
    var groups = {};
    (e.tags || []).forEach(function (id) {
      var t = cidx.id2tag[id];
      if (!t) return;
      var g = t.c1 || '其他';
      if (!groups[g]) groups[g] = [];
      groups[g].push(t);
    });
    return groups;
  }

  return {
    MAXC: MAXC,
    NAME_FIELDS: NAME_FIELDS,
    createIndex: createIndex,
    resolveName: resolveName,
    tagSet: tagSet,
    search: search,
    describe: describe,
    nameFieldsOf: nameFieldsOf
  };
});
