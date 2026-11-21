// Request signing, factor filter schema, and rate-limiting pacing adapted from
// yyahz/umamusume-seed-optimizer (Copyright (c) 2026 yyahz, MIT License).
// Operates strictly within the user's manual login session on https://game.bilibili.com/tool/pd/.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Compliant in-app Mali-Mali (game.bilibili.com/tool/pd/) search bridge and candidate-pool sync.</summary>
internal static class MaliSeedSearchBridge
{
    internal const string OfficialToolUrl = "https://game.bilibili.com/tool/pd/";
    private static MaliToolWindow? activeWindow;
    private static DateTimeOffset nextSearchAt = DateTimeOffset.MinValue;
    private static DateTimeOffset riskCooldownUntil = DateTimeOffset.MinValue;

    private static readonly Dictionary<string, string> RedNameAliases = new(StringComparer.Ordinal)
    {
        ["草"] = "草地",
        ["沙"] = "泥地",
        ["沙地"] = "泥地",
        ["泥"] = "泥地",
        ["短"] = "短距离",
        ["英"] = "英里",
        ["哩"] = "英里",
        ["中"] = "中距离",
        ["长"] = "长距离",
        ["逃"] = "领跑",
        ["逃马"] = "领跑",
        ["领放"] = "领跑",
        ["先"] = "跟前",
        ["先行"] = "跟前",
        ["前列"] = "跟前",
        ["差"] = "居中",
        ["差行"] = "居中",
        ["追"] = "后追",
        ["追马"] = "后追",
    };

    internal static void OpenOrActivate(
        Window? owner,
        Func<JObject, Task<JObject>> backendRequest,
        Func<JObject> getContext,
        Action? onPoolUpdated,
        Action<string>? onWhiteSkillsEdited = null)
    {
        if (activeWindow != null)
        {
            activeWindow.UpdateBindings(backendRequest, getContext, onPoolUpdated, onWhiteSkillsEdited);
            activeWindow.SyncFromDraft(autoUpdateHintOnly: false);
            activeWindow.Activate();
            return;
        }

        var win = new MaliToolWindow(backendRequest, getContext, onPoolUpdated, onWhiteSkillsEdited);
        win.Closed += (_, _) =>
        {
            if (ReferenceEquals(activeWindow, win)) activeWindow = null;
        };
        activeWindow = win;
        if (owner != null) win.Show(owner);
        else win.Show();
    }

    internal static string NormalizeRedName(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return "";
        var clean = raw.Normalize(NormalizationForm.FormKC).Trim();
        if (clean.StartsWith("red:", StringComparison.Ordinal)) clean = clean[4..].Trim();
        else if (clean.StartsWith("pink:", StringComparison.Ordinal)) clean = clean[5..].Trim();
        if (clean.EndsWith("适性", StringComparison.Ordinal) && clean.Length > 2)
            clean = clean[..^2].Trim();
        return RedNameAliases.TryGetValue(clean, out var mapped) ? mapped : clean;
    }

    internal static JObject DeriveSearchSpec(JObject? context)
    {
        var draft = context?["local_route"] as JObject;
        var avoidList = new List<string>();
        var avoidSet = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in (draft?["avoid_a_factors"] as JArray ?? new()).OfType<JObject>())
        {
            var name = NormalizeRedName(Str(item["factor_key"]));
            if (name.Length > 0 && avoidSet.Add(name)) avoidList.Add(name);
        }

        var redList = new List<string>();
        var redSet = new HashSet<string>(StringComparer.Ordinal);
        void AddRed(string? key)
        {
            var name = NormalizeRedName(key);
            if (name.Length > 0 && !avoidSet.Contains(name) && redSet.Add(name))
                redList.Add(name);
        }

        foreach (var goal in (draft?["aptitude_goals"] as JArray ?? new()).OfType<JObject>())
        {
            if (Str(goal["target_rank"]).Length == 0) continue;
            AddRed(Str(goal["factor_key"]));
        }
        foreach (var fk in (draft?["chase_s_factors"] as JArray ?? new()))
            AddRed(Str(fk));

        var nodes = draft?["nodes"] as JArray;
        var hasPlannedParent = false;
        if (nodes != null)
        {
            foreach (var pi in new[] { 0, 3 })
            {
                if (nodes.ElementAtOrDefault(pi) is JObject pNode
                    && Str(pNode["kind"]) == "planned"
                    && pNode["character_id"] is { Type: not JTokenType.Null })
                {
                    hasPlannedParent = true;
                    break;
                }
            }
        }
        var suggestDirt = hasPlannedParent && !avoidSet.Contains("泥地") && !redSet.Contains("泥地");

        var whiteNames = new List<string>();
        var whiteSet = new HashSet<string>(StringComparer.Ordinal);
        void AddWhite(string? raw)
        {
            if (string.IsNullOrWhiteSpace(raw) || whiteNames.Count >= 8) return;
            var clean = raw.Normalize(NormalizationForm.FormKC).Trim();
            if (clean.StartsWith("white:", StringComparison.Ordinal)) clean = clean[6..].Trim();
            if (clean.Length > 0 && whiteSet.Add(clean)) whiteNames.Add(clean);
        }

        foreach (var t in (context?["targets"] as JArray ?? new()).OfType<JObject>())
        {
            var name = Str(t["display_name"]).Length > 0 ? Str(t["display_name"]) : Str(t["name"]);
            if (name.Length == 0 && Str(t["target_id"]) is { Length: > 0 } tid && tid.StartsWith("white:", StringComparison.Ordinal))
                name = tid[6..];
            AddWhite(name);
        }
        foreach (var rt in (context?["raw_targets"] as JArray ?? new()).OfType<JObject>())
            AddWhite(Str(rt["name"]));

        return new JObject
        {
            ["red_factors"] = new JArray(redList),
            ["avoid_red_factors"] = new JArray(avoidList),
            ["white_names"] = new JArray(whiteNames),
            ["suggest_dirt"] = suggestDirt,
        };
    }

    internal static JArray FilterRecordsByAvoidA(JArray records, IEnumerable<string>? avoidRedNames)
    {
        var protectedSet = new HashSet<string>(
            (avoidRedNames ?? Array.Empty<string>()).Select(NormalizeRedName).Where(x => x.Length > 0),
            StringComparer.Ordinal);
        if (protectedSet.Count == 0)
            return (JArray)records.DeepClone();

        var kept = new JArray();
        foreach (var record in records.OfType<JObject>())
        {
            var factors = (record["hero_card"]?["factors"] as JArray ?? new()).OfType<JObject>();
            var polluted = false;
            foreach (var f in factors)
            {
                var fType = Number(f["type"]);
                var fKind = Str(f["kind"]);
                if (fType != 2 && !string.Equals(fKind, "red", StringComparison.Ordinal)) continue;
                var norm = NormalizeRedName(Str(f["name"]).Length > 0 ? Str(f["name"]) : Str(f["factor_name"]));
                if (norm.Length == 0 || !protectedSet.Contains(norm)) continue;
                var selfStars = Number(f["rarity"]) ?? 0;
                var totalStars = Number(f["total_rarity"]) ?? 0;
                if (selfStars > 0 || totalStars > 0)
                {
                    polluted = true;
                    break;
                }
            }
            if (!polluted) kept.Add(record.DeepClone());
        }
        return kept;
    }

    internal static string[] ParseTerms(string? text) =>
        (text ?? "").Split([',', '，', '、', ';', '；', '\n', '\r'], StringSplitOptions.RemoveEmptyEntries)
            .Select(s => s.Trim())
            .Where(s => s.Length > 0)
            .Distinct(StringComparer.Ordinal)
            .ToArray();

    private static string Str(JToken? token) =>
        token?.Type is JTokenType.String or JTokenType.Integer ? token.ToString() : "";

    private static int? Number(JToken? token) =>
        token?.Type == JTokenType.Integer ? (int?)token : null;

    // Reviewed async search function executed only inside https://game.bilibili.com/ with credentials:"include".
    // Never stores credentials and stops cleanly on -101 (login required) or 403/429 (rate limit).
    internal const string MaliSearchAsyncFunction = """
async function runMaaUmaMaliSearch(input) {
  "use strict";
  const PUBLIC_APP_KEY = "d053991039404237a44023da011d3e08";
  const PUBLIC_SIGNING_SALT = "Hs8yIaC/AtYoBEO6jsQuNfDM9nK6ecFaXi2CttwwKxQ=";
  const API_BASE = "https://api.game.bilibili.com";
  const CATALOG_TTL_MS = 15 * 60 * 1000;

  function rotateLeft(value, amount) {
    return (value << amount) | (value >>> (32 - amount));
  }
  function wordHex(value) {
    let result = "";
    for (let byte = 0; byte < 4; byte += 1) {
      result += ((value >>> (byte * 8)) & 0xff).toString(16).padStart(2, "0");
    }
    return result;
  }
  function md5(str) {
    const bytes = new TextEncoder().encode(String(str));
    const totalLength = Math.ceil((bytes.length + 9) / 64) * 64;
    const padded = new Uint8Array(totalLength);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    const bitLength = bytes.length * 8;
    const view = new DataView(padded.buffer);
    view.setUint32(totalLength - 8, bitLength >>> 0, true);
    view.setUint32(totalLength - 4, Math.floor(bitLength / 0x100000000), true);
    let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
    const shifts = [
      7,12,17,22, 7,12,17,22, 7,12,17,22, 7,12,17,22,
      5,9,14,20, 5,9,14,20, 5,9,14,20, 5,9,14,20,
      4,11,16,23, 4,11,16,23, 4,11,16,23, 4,11,16,23,
      6,10,15,21, 6,10,15,21, 6,10,15,21, 6,10,15,21
    ];
    const constants = Array.from({ length: 64 }, (_, i) =>
      Math.floor(Math.abs(Math.sin(i + 1)) * 0x100000000) >>> 0
    );
    for (let offset = 0; offset < padded.length; offset += 64) {
      const words = Array.from({ length: 16 }, (_, i) => view.getUint32(offset + i * 4, true));
      let a = a0, b = b0, c = c0, d = d0;
      for (let i = 0; i < 64; i += 1) {
        let mixed, wordIndex;
        if (i < 16) { mixed = (b & c) | (~b & d); wordIndex = i; }
        else if (i < 32) { mixed = (d & b) | (~d & c); wordIndex = (5 * i + 1) % 16; }
        else if (i < 48) { mixed = b ^ c ^ d; wordIndex = (3 * i + 5) % 16; }
        else { mixed = c ^ (b | ~d); wordIndex = (7 * i) % 16; }
        const nextD = c, nextC = b;
        const sum = (a + mixed + constants[i] + words[wordIndex]) >>> 0;
        const nextB = (b + rotateLeft(sum, shifts[i])) >>> 0;
        a = d; b = nextB; c = nextC; d = nextD;
      }
      a0 = (a0 + a) >>> 0; b0 = (b0 + b) >>> 0; c0 = (c0 + c) >>> 0; d0 = (d0 + d) >>> 0;
    }
    return wordHex(a0) + wordHex(b0) + wordHex(c0) + wordHex(d0);
  }
  function signingValue(value) {
    if (typeof value === "boolean") return value ? "true" : "false";
    return String(value);
  }
  function makeNonce() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
  }
  function signPayload(data) {
    const out = {};
    for (const [key, value] of Object.entries(data || {})) {
      if (value !== null && value !== undefined && value !== "") out[key] = value;
    }
    out.ts = Date.now();
    out.nonce = makeNonce();
    out.appkey = PUBLIC_APP_KEY;
    const unsigned = Object.keys(out).sort().map((k) => `${k}=${signingValue(out[k])}`).join("&");
    out.sign = md5(`${unsigned}&secret=${PUBLIC_SIGNING_SALT}`);
    return out;
  }
  function riskText(value) {
    return /请求.{0,4}(频繁|过快)|访问.{0,4}频繁|操作.{0,4}频繁|风控|稍后再试/i.test(String(value || ""));
  }
  function makeErr(message, opts = {}) {
    const err = new Error(message);
    err.status = opts.status || 0;
    err.code = opts.code || 0;
    err.riskControl = opts.riskControl === true;
    return err;
  }
  async function parseApi(response) {
    const text = await response.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      throw makeErr(`接口返回了非 JSON 内容（HTTP ${response.status}）`, { status: response.status });
    }
    if (response.status === 403 || response.status === 429 || riskText(body?.message)) {
      throw makeErr(body?.message || `触发 B 站接口访问频率限制（HTTP ${response.status}），请等待 60 秒后再试。`, {
        status: response.status,
        riskControl: true
      });
    }
    if (response.status === 401 || body?.code === -101) {
      throw makeErr("请先在下方吗哩吗哩页面手动登录 B 站账号，再点击一键检索。", {
        status: response.status,
        code: -101
      });
    }
    if (!response.ok || body?.code !== 0) {
      throw makeErr(body?.message || `接口请求失败（code=${String(body?.code ?? response.status)}）`, {
        status: response.status,
        code: Number(body?.code) || 0
      });
    }
    return body;
  }

  let lastReqAt = 0;
  async function pace() {
    if (lastReqAt > 0) {
      const targetGap = 350 + Math.floor(Math.random() * 151);
      const waitMs = lastReqAt + targetGap - Date.now();
      if (waitMs > 0) await new Promise((r) => setTimeout(r, waitMs));
    }
    lastReqAt = Date.now();
  }

  function normText(v) {
    return String(v || "").normalize("NFKC").trim().toLowerCase();
  }
  const RED_ALIAS_PAIRS = {
    "泥地": ["泥地", "沙地"],
    "沙地": ["泥地", "沙地"],
    "领跑": ["领跑", "领放", "逃"],
    "跟前": ["跟前", "前列", "先"],
    "居中": ["居中", "差"],
    "后追": ["后追", "追"]
  };

  function flattenFactors(data) {
    let groups = [];
    if (Array.isArray(data)) groups = data[0]?.factor_groups || data;
    else if (data && Array.isArray(data.factor_groups)) groups = data.factor_groups;
    const list = [];
    for (const group of groups || []) {
      for (const f of group?.factors || []) {
        const type = Number(f.type ?? group.type);
        const name = String(f.name || f.num);
        if (!Number.isFinite(type)) continue;
        list.push({ type, num: f.num, name, norm: normText(name) });
      }
    }
    return list;
  }
  function flattenRoles(data) {
    const cards = Array.isArray(data) ? data : (Array.isArray(data?.hero_cards) ? data.hero_cards : []);
    return cards
      .filter((c) => c && c.card_id !== null && c.card_id !== undefined)
      .map((c) => ({ card_id: c.card_id, name: String(c.name || c.card_name || c.card_id), norm: normText(c.name || c.card_name || "") }));
  }

  let cat = globalThis.__maaumaMaliCatalog;
  if (!cat || cat.expiresAt <= Date.now() || !cat.factors?.length || !cat.roles?.length) {
    const fQuery = new URLSearchParams(Object.entries(signPayload({})).map(([k, v]) => [k, String(v)]));
    const fRes = await fetch(`${API_BASE}/game/player/tools/uma/factors?${fQuery}`, { method: "GET", credentials: "include" }).then(parseApi);
    await pace();
    const rQuery = new URLSearchParams(Object.entries(signPayload({})).map(([k, v]) => [k, String(v)]));
    const rRes = await fetch(`${API_BASE}/game/player/tools/uma/hero_cards?${rQuery}`, { method: "GET", credentials: "include" }).then(parseApi);
    const factors = flattenFactors(fRes.data);
    const roles = flattenRoles(rRes.data);
    if (!factors.length || !roles.length) {
      throw makeErr("吗哩吗哩因子或角色目录返回为空，请刷新页面后重试。");
    }
    cat = { factors, roles, expiresAt: Date.now() + CATALOG_TTL_MS };
    globalThis.__maaumaMaliCatalog = cat;
  }

  const unmatchedTerms = [];
  const matchedFactorNames = [];

  const redCatalog = cat.factors.filter((f) => f.type === 2);
  const matchedReds = [];
  for (const rawRed of (Array.isArray(input.redFactors) ? input.redFactors : []).slice(0, 3)) {
    const key = normText(rawRed);
    if (!key) continue;
    const candidates = [key, ...(RED_ALIAS_PAIRS[rawRed] || []).map(normText)];
    let hit = null;
    for (const cand of candidates) {
      hit = redCatalog.find((f) => f.norm === cand);
      if (hit) break;
    }
    if (hit && !matchedReds.some((x) => x.num === hit.num)) {
      matchedReds.push(hit);
      matchedFactorNames.push(hit.name);
    } else if (!hit) {
      unmatchedTerms.push(String(rawRed));
    }
  }

  const whiteCatalog = cat.factors.filter((f) => f.type === 4 || f.type === 5 || f.type === 6);
  const matchedWhites = [];
  for (const rawWhite of (Array.isArray(input.whiteNames) ? input.whiteNames : []).slice(0, 8)) {
    const key = normText(rawWhite);
    if (!key) continue;
    const stripped = key.replace(/[○◎]$/u, "");
    const hit = whiteCatalog.find((f) => f.norm === key)
      || whiteCatalog.find((f) => f.norm === `${stripped}○`)
      || whiteCatalog.find((f) => f.norm === stripped)
      || whiteCatalog.find((f) => f.norm.includes(stripped));
    if (hit && !matchedWhites.some((x) => x.type === hit.type && x.num === hit.num)) {
      matchedWhites.push(hit);
      matchedFactorNames.push(hit.name);
    } else if (!hit) {
      unmatchedTerms.push(String(rawWhite));
    }
  }

  const cardIds = [];
  for (const rawChar of (Array.isArray(input.characterNames) ? input.characterNames : []).slice(0, 8)) {
    const key = normText(rawChar);
    if (!key) continue;
    const exact = cat.roles.filter((r) => r.norm === key);
    const hits = exact.length ? exact : cat.roles.filter((r) => r.norm.includes(key));
    if (!hits.length) {
      unmatchedTerms.push(String(rawChar));
      continue;
    }
    for (const r of hits) {
      if (!cardIds.includes(r.card_id) && cardIds.length < 20) cardIds.push(r.card_id);
    }
  }

  function redFilterEntry(factor, starMode) {
    if (starMode === "self3") return { num: factor.num, self_rarity: 3 };
    if (starMode === "self2") return { num: factor.num, self_rarity: 2 };
    if (starMode === "total9") return { num: factor.num, rarity: 9 };
    return { num: factor.num, rarity: 6 };
  }
  function buildFilterGroups(redFactor, whites, starMode) {
    const byType = new Map();
    if (redFactor) {
      byType.set(redFactor.type, [redFilterEntry(redFactor, starMode)]);
    }
    for (const w of whites) {
      if (!byType.has(w.type)) byType.set(w.type, []);
      byType.get(w.type).push({ num: w.num, rarity: 1 });
    }
    return [...byType.entries()].map(([type, values]) => ({ type, values }));
  }

  const starMode = String(input.starMode || "self3");
  const filterFull = input.filterFollowReachLimit === false ? 0 : 1;
  const maxPages = Math.min(2, Math.max(1, Number(input.maxPages) || 1));

  // Hard cap: max 5 HTTP search requests per click to respect official rate limits.
  const queryPlan = [];
  if (matchedWhites.length === 0) {
    const reds = matchedReds.length ? matchedReds : [null];
    for (const r of reds) {
      queryPlan.push({ filters: buildFilterGroups(r, [], starMode), pages: maxPages });
    }
  } else {
    const primaryReds = matchedReds.length ? matchedReds.slice(0, 2) : [null];
    const comboWhites = matchedWhites.slice(0, 2);
    for (const r of primaryReds) {
      const pages = matchedWhites.length === 1 ? maxPages : 1;
      queryPlan.push({ filters: buildFilterGroups(r, comboWhites, starMode), pages });
    }
    if (matchedWhites.length > 1) {
      const leadRed = primaryReds[0];
      for (const w of matchedWhites.slice(0, 4)) {
        if (queryPlan.length >= 5) break;
        queryPlan.push({ filters: buildFilterGroups(leadRed, [w], starMode), pages: 1 });
      }
    }
  }

  const recordsById = new Map();
  let queriedCount = 0;

  for (const plan of queryPlan) {
    if (queriedCount >= 5) break;
    for (let page = 1; page <= plan.pages; page += 1) {
      if (queriedCount >= 5) break;
      await pace();
      const body = signPayload({
        card_ids: cardIds.length ? cardIds.join(",") : "",
        filter_follow_reach_limit: filterFull,
        min_win_race_count: "",
        factor_filters: plan.filters.length ? JSON.stringify(plan.filters) : "",
        page_size: 20,
        page_num: page
      });
      const res = await fetch(`${API_BASE}/game/player/tools/uma/hero_card/search`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(parseApi);
      queriedCount += 1;
      const pageRecords = Array.isArray(res?.data?.records) ? res.data.records : [];
      for (const rec of pageRecords) {
        const rid = String(rec?.role_id ?? "");
        if (!rid || recordsById.has(rid)) continue;
        if (recordsById.size >= 120) break;
        const hc = rec?.hero_card || {};
        recordsById.set(rid, {
          role_id: rec.role_id,
          hero_card: {
            card_id: hc.card_id ?? null,
            name: String(hc.name || hc.card_name || ""),
            win_race_count: hc.win_race_count ?? null,
            factors: Array.isArray(hc.factors)
              ? hc.factors.map((f) => ({
                  type: Number(f.type ?? 0),
                  name: String(f.name || f.factor_name || ""),
                  rarity: Number(f.rarity ?? 0),
                  total_rarity: Number(f.total_rarity ?? 0)
                }))
              : []
          }
        });
      }
      if (!res?.data?.has_next_page || recordsById.size >= 120) break;
    }
    if (recordsById.size >= 120) break;
  }

  const desiredWhiteNorms = matchedWhites.map((w) => ({
    key: `${w.type}:${normText(w.name).replace(/[○◎]$/u, "")}`,
    stripped: normText(w.name).replace(/[○◎]$/u, ""),
    label: w.name
  }));
  const desiredRedNorms = new Set(matchedReds.map((r) => normText(r.name)));

  function scoreRecord(rec) {
    const factors = rec?.hero_card?.factors || [];
    let whiteTotalCount = 0;
    let whiteSelfCount = 0;
    let whiteTotalStars = 0;
    let whiteSelfStars = 0;
    let redTotalStars = 0;
    const redSummary = [];
    const matchedWhiteLabels = [];

    for (const f of factors) {
      const fn = normText(f.name);
      const fnStripped = fn.replace(/[○◎]$/u, "");
      if (f.type === 2 && ((f.rarity || 0) > 0 || (f.total_rarity || 0) > 0)) {
        if (!desiredRedNorms.size || desiredRedNorms.has(fn)) {
          redTotalStars += Number(f.total_rarity || f.rarity || 0);
        }
        if ((f.rarity || 0) > 0 || desiredRedNorms.has(fn)) {
          redSummary.push(`${f.name}${f.rarity ? `本体${f.rarity}★/` : ""}家系${f.total_rarity || 0}★`);
        }
      }
      if (f.type === 4 || f.type === 5 || f.type === 6) {
        const hitW = desiredWhiteNorms.find((w) => w.stripped === fnStripped);
        if (hitW) {
          const ownS = Number(f.rarity || 0);
          const totS = Number(f.total_rarity || ownS);
          if (totS > 0 || ownS > 0) {
            whiteTotalCount += 1;
            whiteTotalStars += totS;
            if (ownS > 0) {
              whiteSelfCount += 1;
              whiteSelfStars += ownS;
            }
            matchedWhiteLabels.push(`${f.name}${ownS > 0 ? `本体${ownS}★/` : ""}家系${totS}★`);
          }
        }
      }
    }
    const winCount = Number(rec?.hero_card?.win_race_count || 0);
    return {
      rec,
      whiteTotalCount,
      whiteSelfCount,
      whiteTotalStars,
      whiteSelfStars,
      redTotalStars,
      winCount,
      redSummary: redSummary.slice(0, 2).join(" · "),
      matchedWhiteLabels
    };
  }

  const scored = [...recordsById.values()].map(scoreRecord).sort((a, b) =>
    (b.whiteTotalCount - a.whiteTotalCount)
    || (b.whiteSelfCount - a.whiteSelfCount)
    || (b.whiteTotalStars - a.whiteTotalStars)
    || (b.whiteSelfStars - a.whiteSelfStars)
    || (b.redTotalStars - a.redTotalStars)
    || (b.winCount - a.winCount)
    || String(a.rec.role_id).localeCompare(String(b.rec.role_id))
  );

  const topScored = scored.slice(0, 40);
  return {
    records: topScored.map((s) => s.rec),
    preview: topScored.slice(0, 8).map((s) => ({
      role_id: s.rec.role_id,
      name: s.rec.hero_card.name,
      red_summary: s.redSummary,
      matched_whites: s.matchedWhiteLabels,
      white_hits: s.whiteTotalCount,
      white_total_stars: s.whiteTotalStars
    })),
    queried_count: queriedCount,
    matched_factors: matchedFactorNames,
    unmatched_terms: unmatchedTerms
  };
}
""";

    private sealed class MaliToolWindow : Window
    {
        private Func<JObject, Task<JObject>> backendRequest;
        private Func<JObject> getContext;
        private Action? onPoolUpdated;
        private Action<string>? onWhiteSkillsEdited;

        private readonly UmaParentPlannerBrowser browser;
        private readonly ComboBox redModeBox = new()
        {
            Name = "MaliSearchRedMode",
            ItemsSource = new[]
            {
                "按当前草稿自动拆分", "不限红因子",
                "中距离", "泥地", "英里", "长距离", "短距离", "草地", "领跑", "跟前", "居中", "后追",
            },
            SelectedIndex = 0,
            Width = 175,
        };
        private readonly ComboBox starModeBox = new()
        {
            Name = "MaliSearchStarMode",
            ItemsSource = new[] { "本体 3★（推荐）", "家系 ≥ 6★", "家系 ≥ 9★", "本体 ≥ 2★" },
            SelectedIndex = 0,
            Width = 150,
        };
        private readonly ComboBox pagesBox = new()
        {
            Name = "MaliSearchPages",
            ItemsSource = new[] { "1 页（最多 20 匹）", "2 页（最多 40 匹）" },
            SelectedIndex = 0,
            Width = 155,
        };
        private readonly CheckBox includeDirtCheck = new() { Name = "MaliSearchIncludeDirt", Content = "顺带补泥地（改泥刷相性）", IsChecked = false, VerticalAlignment = VerticalAlignment.Center };
        private readonly CheckBox filterFullCheck = new() { Name = "MaliSearchFilterFull", Content = "过滤关注已满", IsChecked = true, VerticalAlignment = VerticalAlignment.Center };
        private readonly CheckBox filterPollutedCheck = new() { Name = "MaliSearchFilterPolluted", Content = "自动过滤禁升A污染", IsChecked = true, VerticalAlignment = VerticalAlignment.Center };
        private readonly TextBox characterFilterBox = new() { Name = "MaliSearchCharBox", Watermark = "角色过滤（可留空，不限马；或填 小栗帽、美浦波旁）", MinWidth = 260 };
        private readonly TextBox whiteFilterBox = new() { Name = "MaliSearchWhiteBox", Watermark = "白/技能因子（最多 8 项，逗号分隔，如 地固、弯道加速、川崎赛场、逆时针）", MinWidth = 260 };
        private readonly Button homeButton = new() { Name = "MaliGoHome", Content = "回到吗哩页面" };
        private readonly Button openSystemBrowserButton = new() { Name = "MaliOpenSystemBrowser", Content = "系统浏览器打开 ↗" };
        private readonly Button syncButton = new() { Name = "MaliSyncDraft", Content = "从当前系谱同步条件" };
        private readonly Button searchSaveButton = new() { Name = "MaliSearchAndSave", Content = "🔍 一键检索并存入候选池" };
        private readonly Button clearPoolButton = new() { Name = "MaliClearPool", Content = "清空候选池" };
        private readonly StackPanel previewList = new() { Name = "MaliSearchPreviewList", Spacing = 4 };
        private readonly Expander previewExpander;
        private readonly TextBox fallbackPasteBox = new() { Name = "MaliFallbackPasteBox", Watermark = "若内嵌页暂不可用，可在系统浏览器筛选后复制卡片文字或响应 JSON 粘贴到这里，点右侧导入入池", AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxLength = 262144, MinHeight = 56, MaxHeight = 110 };
        private readonly Button fallbackImportButton = new() { Name = "MaliFallbackImportButton", Content = "导入粘贴内容入池", FontSize = 12 };
        private readonly Expander fallbackExpander;
        private readonly TextBlock draftSummaryText = Note("");
        private readonly TextBlock statusText = Note("正在加载吗哩吗哩官网…如尚未登录，请先在下方网页手动登录 B 站账号。");
        private bool busy;
        private bool syncingWhiteBox;

        public MaliToolWindow(
            Func<JObject, Task<JObject>> backendRequest,
            Func<JObject> getContext,
            Action? onPoolUpdated,
            Action<string>? onWhiteSkillsEdited = null)
        {
            this.backendRequest = backendRequest;
            this.getContext = getContext;
            this.onPoolUpdated = onPoolUpdated;
            this.onWhiteSkillsEdited = onWhiteSkillsEdited;

            Title = "吗哩吗哩种马检索 · 本地候选池联动";
            Width = 1120;
            Height = 840;
            MinWidth = 820;
            MinHeight = 640;
            WindowStartupLocation = WindowStartupLocation.CenterOwner;
            this.Bind(TextElement.ForegroundProperty, new DynamicResourceExtension("SukiText"));
            this.Bind(BackgroundProperty, new DynamicResourceExtension("SukiStrongBackground"));

            var profile = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "MaaUma", "web-tools", "parent-planner-mali");
            browser = UmaParentPlannerBrowser.ForMaliTool(profile);
            browser.StatusChanged += text =>
            {
                if (!busy) statusText.Text = text;
                if (text.Contains("启动失败", StringComparison.Ordinal))
                    fallbackExpander.IsExpanded = true;
            };

            var actionRow = new WrapPanel { Orientation = Orientation.Horizontal };
            foreach (var btn in new[] { homeButton, openSystemBrowserButton, syncButton, searchSaveButton, clearPoolButton })
            {
                btn.Margin = new Thickness(0, 0, 8, 4);
                actionRow.Children.Add(btn);
            }

            var optionRow = new WrapPanel { Orientation = Orientation.Horizontal };
            foreach (var ctrl in new Control[] { redModeBox, starModeBox, pagesBox, includeDirtCheck, filterFullCheck, filterPollutedCheck })
            {
                ctrl.Margin = new Thickness(0, 0, 10, 4);
                optionRow.Children.Add(ctrl);
            }

            var textFilterRow = new Grid
            {
                ColumnDefinitions = new("*,*"),
                ColumnSpacing = 8,
            };
            textFilterRow.Children.Add(characterFilterBox);
            Grid.SetColumn(whiteFilterBox, 1);
            textFilterRow.Children.Add(whiteFilterBox);

            previewExpander = new Expander
            {
                Name = "MaliSearchPreviewExpander",
                Header = "最近检索白技能命中预览（0 匹）",
                IsExpanded = false,
                Content = new ScrollViewer { Content = previewList, MaxHeight = 160 },
            };

            fallbackExpander = new Expander
            {
                Name = "MaliFallbackPasteExpander",
                Header = "备选：粘贴吗哩卡片 / JSON 直接入池（免内嵌页兜底）",
                IsExpanded = false,
                Content = new StackPanel
                {
                    Spacing = 5,
                    Children = { fallbackPasteBox, fallbackImportButton },
                },
            };

            var credit = Note("仅使用你在下方手动登录后的 B 站官方页面会话；节流与签名协议参考 MIT 项目 yyahz/umamusume-seed-optimizer。");
            credit.Opacity = 0.78;
            credit.FontSize = 11;

            var topStack = new StackPanel
            {
                Spacing = 5,
                Children = { actionRow, optionRow, textFilterRow, draftSummaryText, statusText, previewExpander, fallbackExpander, credit },
            };
            var topCard = new Border
            {
                Padding = new Thickness(12, 9),
                BorderThickness = new Thickness(0, 0, 0, 1),
                Child = topStack,
            };
            topCard.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
            topCard.Bind(Border.BackgroundProperty, new DynamicResourceExtension("SukiStrongBackground"));

            var root = new Grid { RowDefinitions = new("Auto,*") };
            root.Children.Add(topCard);
            Grid.SetRow(browser, 1);
            root.Children.Add(browser);
            Content = root;

            homeButton.Click += (_, _) => browser.Navigate(OfficialToolUrl);
            openSystemBrowserButton.Click += (_, _) =>
            {
                try { using var _ = Process.Start(new ProcessStartInfo(OfficialToolUrl) { UseShellExecute = true }); }
                catch { statusText.Text = "未能调起系统浏览器，可手动访问 " + OfficialToolUrl; }
            };
            syncButton.Click += (_, _) => SyncFromDraft(autoUpdateHintOnly: false);
            searchSaveButton.Click += async (_, _) => await RunSearchAndSaveAsync();
            clearPoolButton.Click += async (_, _) => await ClearPoolAsync();
            fallbackImportButton.Click += async (_, _) => await ImportFallbackPasteAsync();
            whiteFilterBox.TextChanged += (_, _) =>
            {
                if (!syncingWhiteBox) onWhiteSkillsEdited?.Invoke(whiteFilterBox.Text ?? "");
            };

            SyncFromDraft(autoUpdateHintOnly: false);
            browser.Navigate(OfficialToolUrl);
        }

        public void UpdateBindings(
            Func<JObject, Task<JObject>> backendRequest,
            Func<JObject> getContext,
            Action? onPoolUpdated,
            Action<string>? onWhiteSkillsEdited = null)
        {
            this.backendRequest = backendRequest;
            this.getContext = getContext;
            this.onPoolUpdated = onPoolUpdated;
            if (onWhiteSkillsEdited != null) this.onWhiteSkillsEdited = onWhiteSkillsEdited;
        }

        public void SyncFromDraft(bool autoUpdateHintOnly)
        {
            var spec = DeriveSearchSpec(SafeContext());
            var reds = (spec["red_factors"] as JArray ?? new()).Select(Str).Where(x => x.Length > 0).ToArray();
            var avoids = (spec["avoid_red_factors"] as JArray ?? new()).Select(Str).Where(x => x.Length > 0).ToArray();
            var whites = (spec["white_names"] as JArray ?? new()).Select(Str).Where(x => x.Length > 0).ToArray();
            var suggestDirt = (bool?)spec["suggest_dirt"] == true;

            if (!autoUpdateHintOnly && !busy)
            {
                includeDirtCheck.IsChecked = suggestDirt;
                syncingWhiteBox = true;
                try
                {
                    if (whites.Length > 0) whiteFilterBox.Text = string.Join("，", whites);
                }
                finally { syncingWhiteBox = false; }
            }

            var redPart = reds.Length > 0 ? "红目标：" + string.Join(" / ", reds) : "红目标：未指定";
            var avoidPart = avoids.Length > 0 ? "🔒 防污染排除：" + string.Join(" / ", avoids) : "防污染排除：无";
            var whitePart = whites.Length > 0 ? $" · 白技能：{string.Join(" / ", whites.Take(4))}{(whites.Length > 4 ? "…" : "")}" : "";
            var dirtPart = suggestDirt ? " · 💡 检测到待培养亲代，建议顺带补泥地" : "";
            draftSummaryText.Text = $"{redPart} · {avoidPart}{whitePart}{dirtPart}";
        }

        private JObject SafeContext()
        {
            try { return getContext(); }
            catch { return new JObject(); }
        }

        private void SetBusy(bool value)
        {
            busy = value;
            searchSaveButton.IsEnabled = syncButton.IsEnabled = clearPoolButton.IsEnabled = fallbackImportButton.IsEnabled = !value;
        }

        private void RenderPreview(JArray? preview)
        {
            previewList.Children.Clear();
            var rows = (preview ?? new JArray()).OfType<JObject>().ToArray();
            previewExpander.Header = $"最近检索白技能命中预览（{rows.Length} 匹）";
            foreach (var row in rows)
            {
                var name = Str(row["name"]);
                var rid = Str(row["role_id"]);
                var redSum = Str(row["red_summary"]);
                var whites = (row["matched_whites"] as JArray ?? new()).Select(Str).Where(x => x.Length > 0).ToArray();
                var line = $"{name}（ID {rid}）";
                if (redSum.Length > 0) line += " · 红：" + redSum;
                if (whites.Length > 0) line += " · 🎯白：" + string.Join(" / ", whites);
                previewList.Children.Add(Note(line));
            }
            previewExpander.IsExpanded = rows.Length > 0;
        }

        private async Task ImportFallbackPasteAsync()
        {
            if (busy) return;
            var raw = (fallbackPasteBox.Text ?? "").Trim();
            if (raw.Length == 0)
            {
                statusText.Text = "请先在备选框粘贴吗哩种马卡片文字或官方搜索结果 JSON。";
                return;
            }
            SetBusy(true);
            try
            {
                var ctx = SafeContext();
                var cmd = new JObject
                {
                    ["action"] = "parent_planning_external_compare",
                    ["mode"] = "pool_save",
                    ["candidates"] = raw,
                };
                if (ctx["library_id"] is JToken lib && lib.Type != JTokenType.Null) cmd["library_id"] = lib.DeepClone();
                if (ctx["account_id"] is JToken acc && acc.Type != JTokenType.Null) cmd["account_id"] = acc.DeepClone();
                var reply = await backendRequest(cmd);
                var added = Number(reply["added_count"]) ?? 0;
                var total = Number(reply["pool_count"]) ?? 0;
                fallbackPasteBox.Text = "";
                statusText.Text = $"✅ 已导入并存入候选池（新增 {added} 匹，池内共 {total} 匹）。回到主界面点『一键推演三档方案』即可选用！";
                onPoolUpdated?.Invoke();
            }
            catch (InvalidOperationException ex)
            {
                statusText.Text = ex.Message;
            }
            catch
            {
                statusText.Text = "备选导入失败，请确认粘贴内容为种马卡片文字或搜索 JSON。";
            }
            finally
            {
                SetBusy(false);
            }
        }

        private async Task RunSearchAndSaveAsync()
        {
            if (busy) return;
            var now = DateTimeOffset.UtcNow;
            if (now < riskCooldownUntil)
            {
                var sec = Math.Max(1, (int)Math.Ceiling((riskCooldownUntil - now).TotalSeconds));
                statusText.Text = $"触发 B 站接口频控冷却，请等待 {sec} 秒后再检索。";
                return;
            }
            if (now < nextSearchAt)
            {
                var sec = Math.Max(1, (int)Math.Ceiling((nextSearchAt - now).TotalSeconds));
                statusText.Text = $"为保护账号请求频率，请稍等 {sec} 秒再点检索。";
                return;
            }

            var ctx = SafeContext();
            var spec = DeriveSearchSpec(ctx);
            var avoidNames = (spec["avoid_red_factors"] as JArray ?? new()).Select(Str).Where(x => x.Length > 0).ToArray();
            var avoidSet = new HashSet<string>(avoidNames.Select(NormalizeRedName), StringComparer.Ordinal);

            var redList = new List<string>();
            var modeIdx = redModeBox.SelectedIndex;
            if (modeIdx == 0)
            {
                redList.AddRange((spec["red_factors"] as JArray ?? new()).Select(Str).Where(x => x.Length > 0));
            }
            else if (modeIdx >= 2 && redModeBox.SelectedItem is string fixedRed)
            {
                redList.Add(fixedRed);
            }

            if (includeDirtCheck.IsChecked == true && !avoidSet.Contains("泥地") && !redList.Contains("泥地", StringComparer.Ordinal))
                redList.Add("泥地");

            var starMode = starModeBox.SelectedIndex switch
            {
                1 => "total6",
                2 => "total9",
                3 => "self2",
                _ => "self3",
            };
            var maxPages = pagesBox.SelectedIndex == 1 ? 2 : 1;
            var whiteTerms = ParseTerms(whiteFilterBox.Text);
            var charTerms = ParseTerms(characterFilterBox.Text);

            var jsPayload = new JObject
            {
                ["redFactors"] = new JArray(redList.Take(3)),
                ["whiteNames"] = new JArray(whiteTerms.Take(8)),
                ["characterNames"] = new JArray(charTerms.Take(8)),
                ["starMode"] = starMode,
                ["filterFollowReachLimit"] = filterFullCheck.IsChecked != false,
                ["maxPages"] = maxPages,
            };

            SetBusy(true);
            statusText.Text = whiteTerms.Length > 0
                ? $"正在通过已登录会话多维度拆查白技能【{string.Join(" / ", whiteTerms.Take(4))}】（自动按命中数排序）…"
                : redList.Count > 0
                    ? $"正在通过已登录会话按【{string.Join(" / ", redList.Take(3))}】温和检索（含间隔保护）…"
                    : "正在通过已登录会话按当前条件温和检索…";

            try
            {
                var job = await browser.ExecuteMaliJobAsync(MaliSearchAsyncFunction, jsPayload, TimeSpan.FromSeconds(24));
                if ((bool?)job["ok"] != true)
                {
                    var err = job["error"] as JObject;
                    if ((bool?)err?["riskControl"] == true)
                        riskCooldownUntil = DateTimeOffset.UtcNow.AddSeconds(60);
                    statusText.Text = Str(err?["message"]) is { Length: > 0 } msg ? msg : "检索未成功，请确认已在下方登录 B 站账号。";
                    return;
                }

                nextSearchAt = DateTimeOffset.UtcNow.AddSeconds(2);
                var data = job["data"] as JObject ?? new JObject();
                var rawRecords = data["records"] as JArray ?? new JArray();
                var filtered = filterPollutedCheck.IsChecked != false
                    ? FilterRecordsByAvoidA(rawRecords, avoidNames)
                    : (JArray)rawRecords.DeepClone();
                var dropped = rawRecords.Count - filtered.Count;
                var unmatched = (data["unmatched_terms"] as JArray ?? new()).Select(Str).Where(x => x.Length > 0).ToArray();
                var warnSuffix = unmatched.Length > 0 ? $"（未匹配词：{string.Join("、", unmatched)}）" : "";

                RenderPreview(data["preview"] as JArray);

                if (filtered.Count == 0)
                {
                    statusText.Text = rawRecords.Count == 0
                        ? $"官方接口返回 0 匹匹配候选{warnSuffix}；可放宽星数档位或减少白因子筛选词再试。"
                        : $"官方接口返回 {rawRecords.Count} 匹，但全部触发防污染排除（{string.Join("、", avoidNames)}）已被过滤{warnSuffix}。";
                    return;
                }

                var cmd = new JObject
                {
                    ["action"] = "parent_planning_external_compare",
                    ["mode"] = "pool_save",
                    ["candidates"] = new JObject
                    {
                        ["code"] = 0,
                        ["data"] = new JObject { ["records"] = filtered },
                    },
                };
                if (ctx["library_id"] is JToken lib && lib.Type != JTokenType.Null) cmd["library_id"] = lib.DeepClone();
                if (ctx["account_id"] is JToken acc && acc.Type != JTokenType.Null) cmd["account_id"] = acc.DeepClone();

                var saveReply = await backendRequest(cmd);
                var added = Number(saveReply["added_count"]) ?? 0;
                var total = Number(saveReply["pool_count"]) ?? 0;
                var droppedPart = dropped > 0 ? $"（已拦截防污染 {dropped} 匹）" : "";
                statusText.Text = $"✅ 检索并筛选 {rawRecords.Count} 匹{droppedPart}，新增 {added} 匹写入本地候选池（池内共 {total} 匹）{warnSuffix}。回到主界面勾上『祖辈优先白技能』再点『一键推演三档方案』！";
                onPoolUpdated?.Invoke();
            }
            catch (InvalidOperationException ex)
            {
                statusText.Text = ex.Message;
                if (!string.IsNullOrWhiteSpace(browser.LastInitError)) fallbackExpander.IsExpanded = true;
            }
            catch
            {
                statusText.Text = "检索或写入候选池失败，请确认已手动登录 B 站且未满 200 匹上限。";
            }
            finally
            {
                SetBusy(false);
            }
        }

        private async Task ClearPoolAsync()
        {
            if (busy) return;
            SetBusy(true);
            try
            {
                var ctx = SafeContext();
                var cmd = new JObject
                {
                    ["action"] = "parent_planning_external_compare",
                    ["mode"] = "pool_clear",
                };
                if (ctx["library_id"] is JToken lib && lib.Type != JTokenType.Null) cmd["library_id"] = lib.DeepClone();
                if (ctx["account_id"] is JToken acc && acc.Type != JTokenType.Null) cmd["account_id"] = acc.DeepClone();
                await backendRequest(cmd);
                onPoolUpdated?.Invoke();
                statusText.Text = "已清空本地吗哩候选池，可重新一键检索。";
            }
            catch (InvalidOperationException ex)
            {
                statusText.Text = ex.Message;
            }
            catch
            {
                statusText.Text = "清空候选池失败，请稍后重试。";
            }
            finally
            {
                SetBusy(false);
            }
        }

        private static TextBlock Note(string text) => new() { Text = text, FontSize = 12, TextWrapping = TextWrapping.Wrap };
    }
}
