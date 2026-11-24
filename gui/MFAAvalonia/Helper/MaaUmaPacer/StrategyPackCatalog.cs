using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed record WeightPresetEntry(
    string Id,
    string Name,
    string Summary,
    string? RecommendedRevision,
    string ProfileJson,
    string PackId
);

public sealed record SkillLadderPresetEntry(
    string Id,
    string Name,
    string Summary,
    string PolicyJson,
    string PackId
);

public sealed record StrategyRevisionEntry(
    string Revision,
    string Label,
    string Notice,
    string DefaultFailureLimit,
    double AfterTargetCredit,
    double SpeedWeight,
    double StaminaWeight,
    double PowerWeight,
    double GutsWeight,
    double WisdomWeight,
    string Family,
    bool AutoRouted,
    bool FromExternalPack,
    IReadOnlyList<string> Aliases
);

/// <summary>
/// Dynamic strategy preset catalog reading the public baseline (&lt;= 2.3) plus any
/// installed external strategy pack manifests (schema_version == maauma.strategy_pack.v1).
/// </summary>
public static class StrategyPackCatalog
{
    internal const string BaselineDefaultRevision = "2.3";

    internal const string B198Label = "1.9.8 · 首年体力与羁绊窗口";
    internal const string B198Notice = "首年在统一估值 B 上先比较各行动的体力结果，再按跨80羁绊的训练机会调整选择。黄帽内置前半+5、后半+3，手动额外分默认0，明确保存的值继续叠加。第二、三年沿用1.9.3策略。主动选择后用于新建育成。";
    internal const string B199Label = "1.9.9 · 远征训练与采购（试用）";
    internal const string B199Notice = "首年沿用1.9.8。末段远征按当手首选训练安排采购，部分回合同时比较下一手投资。最后两手降低明显超出目标的属性收益，沿用现有技能Pt估值和手动折扣。主动选择后用于新建育成，可回退1.9.8。";
    internal const string B1910Label = "1.9.10 · 中期训练与SS时机（试用）";
    internal const string B1910Notice = "首次凯旋门后，结合接下来两手的国内训练比较先训练或先开SS。已确认的SS回复计入体力估值，保留的SS属性与技能Pt按一次行动计算。继承1.9.9的首年和远征采购，主动选择后用于新建育成，可回退1.9.9。";
    internal const string B1911Label = "1.9.11 · 备用满组衔接（试用）";
    internal const string B1911Notice = "首凯后的国内训练回合，若已确认当前SS组之外还有足够成员满充，会在比较训练和开SS时考虑这个备用满组。其余情况沿用1.9.10。主动选择后用于新建育成，可回退1.9.10。";
    internal const string B1912Label = "1.9.12 · 首次远征衔接";
    internal const string B1912Notice = "二年首次远征前，比较可支付的适应性升级组合与入营首课，并考虑体力衔接。使用本局设置的训练失败率上限。策略用于新建育成，可回退1.9.11。";
    internal const string B20Label = "2.0 · 二远条件采购";
    internal const string B20Notice = "二年远征时，按当手和后续课面比较设施何时购买，并把胆魄的减耗纳入报价。实际购买按届时课面与适应性积分重新判断。用于新建育成，可回退1.9.12。";
    internal const string B21Label = "2.1 · 首远按课采购";
    internal const string B21Notice = "首次远征买满 Pt 和至少一项训练设施后，若直接训练得分更高、风险不增加且条件 AP 收入不减少，会把保留现金的方案纳入报价。额外设施有当手收益时仍参与比较。可切回 2.0。";
    internal const string B22Label = "2.2 · 配卡采购与体力精算";
    internal const string B22Notice = "首远按入营预算硬锁零卡属性Lv3（高预算局锁第1槽配卡主属性后开放第2槽灵活收益），远征前3回合按黄帽与胆魄Lv3动态控入营体力带，仅限5人满团必胜SS对平庸耗体课启用省体开启，三年级补短板计入二远与终盘结算并平滑1180耐力过渡。可切回 2.1。";
    internal const string B23Label = "2.3 · 开源基线强化版";
    internal const string B23Notice = "融合纯策略底座与 15 维评分权重（内置 28% 默认失败率上限、黄金船 1.09 / 佐岳梅 1.18 偏好及首远非彩圈必买 Pt50 修复），无需手动导入权重 JSON，直接选择即可对下次新开局生效。可随时切回 2.2。";

    private static readonly StrategyRevisionEntry[] BaselineEntries =
    {
        new("2.3", B23Label, B23Notice, "28", 0.5, 1.0, 1.0, 1.25, 0.85, 1.0, "<=2.3", false, false, new[] { "2.3_pro", "unified_v2_3", "unified_v2_3_pro" }),
        new("2.2", B22Label, B22Notice, "25", 0.5, 1.0, 1.0, 1.25, 0.85, 1.0, "<=2.3", false, false, new[] { "unified_v2_2" }),
        new("2.1", B21Label, B21Notice, "25", 0.5, 1.0, 1.0, 1.25, 0.85, 1.0, "<=2.3", false, false, new[] { "unified_v2_1" }),
        new("2.0", B20Label, B20Notice, "25", 0.5, 1.0, 1.0, 1.25, 0.85, 1.0, "<=2.3", false, false, Array.Empty<string>()),
    };

    private static readonly object SyncRoot = new();
    private static Snapshot _current = BuildSnapshot();

    private sealed record Snapshot(
        bool HasExternalPack,
        string LoadedPackSummary,
        string DefaultRevision,
        StrategyRevisionEntry[] OrderedChoices,
        string[] OrderedRevisions,
        Dictionary<string, StrategyRevisionEntry> Lookup,
        string[] UnlockedWorkflowModeIds,
        HashSet<string> UnlockedWorkflowModeSet,
        bool AllowCustomWorkflowModes,
        WeightPresetEntry[] WeightPresets,
        SkillLadderPresetEntry[] SkillLadderPresets
    );

    public static bool HasExternalPack { get { lock (SyncRoot) return _current.HasExternalPack; } }
    public static string LoadedPackSummary { get { lock (SyncRoot) return _current.LoadedPackSummary; } }
    public static string DefaultRevision { get { lock (SyncRoot) return _current.DefaultRevision; } }
    public static StrategyRevisionEntry[] Choices { get { lock (SyncRoot) return _current.OrderedChoices; } }
    public static string[] Revisions { get { lock (SyncRoot) return _current.OrderedRevisions; } }
    public static IReadOnlyList<string> UnlockedWorkflowModeIds { get { lock (SyncRoot) return _current.UnlockedWorkflowModeIds; } }
    public static bool AllowCustomWorkflowModes { get { lock (SyncRoot) return _current.AllowCustomWorkflowModes; } }
    public static WeightPresetEntry[] WeightPresets { get { lock (SyncRoot) return _current.WeightPresets; } }
    public static IReadOnlyList<SkillLadderPresetEntry> SkillLadderPresets { get { lock (SyncRoot) return _current.SkillLadderPresets; } }

    public static bool IsWorkflowModeUnlocked(string? modeId)
    {
        if (string.Equals(modeId, "direct_career", StringComparison.Ordinal))
            return true;
        if (string.IsNullOrWhiteSpace(modeId))
            return false;
        lock (SyncRoot)
        {
            return _current.UnlockedWorkflowModeSet.Contains(modeId);
        }
    }
    public static int FactoryStrategyIndex
    {
        get
        {
            lock (SyncRoot)
            {
                var idx = Array.IndexOf(_current.OrderedRevisions, _current.DefaultRevision);
                return idx >= 0 ? idx + 1 : 1;
            }
        }
    }

    public static void Reload()
    {
        var next = BuildSnapshot();
        lock (SyncRoot)
        {
            _current = next;
        }
    }

    public static string[] StrategyItems(string retainLabel)
    {
        Snapshot snap;
        lock (SyncRoot) snap = _current;
        return snap.OrderedChoices
            .Select(c => c.Revision == snap.DefaultRevision ? c.Label + " · 默认" : c.Label)
            .Prepend(retainLabel)
            .ToArray();
    }

    public static StrategyRevisionEntry? Find(string? revision)
    {
        if (string.IsNullOrWhiteSpace(revision)) return null;
        lock (SyncRoot)
        {
            return _current.Lookup.TryGetValue(revision.Trim(), out var entry) ? entry : null;
        }
    }

    public static string GetNotice(string? revision) =>
        TryGetKnownNotice(revision, out var notice)
            ? notice
            : " 当前配置使用的是扩展或外部策略版本；如未安装对应策略包，后端将提示版本未注册。";

    public static bool TryGetKnownNotice(string? revision, out string notice)
    {
        if (string.IsNullOrWhiteSpace(revision))
        {
            notice = "";
            return false;
        }
        var entry = Find(revision);
        if (entry != null)
        {
            notice = entry.Notice;
            return true;
        }
        var legacy = revision.Trim() switch
        {
            "1.9.12" => B1912Notice,
            "1.9.11" => B1911Notice,
            "1.9.10" => B1910Notice,
            "1.9.9" => B199Notice,
            "1.9.8" => B198Notice,
            "1.9.7" => "1.9.7 首年采用统一估值 B，训练、恢复和满员SS按同一套边际价值比较。数值、羁绊和充电倍率以及在场额外分各作用一次；黄帽内置首年前半+5、后半+3，手动额外分默认0，明确保存的值继续叠加。首年的高级细调、公式及分段策略评分暂存；第二、三年沿用1.9.3能力和已保存设置。试用只对主动选择后新建的Goal生效。",
            "1.9.6" => "沿用1.9.5的权重、末训目标缺口和首年SS循环。0.5.23的三项修复适用于已有策略。餐食角色身份缺失时可按精确标题和一致效果匹配，当前体力缺口超过10才选下。训练头像漏读时保留已确认的参与者和黄帽收益，未确认项保持未知。远征报价在同场景、同等级且资金增加时保留已知可行路线。主动选择1.9.6仅用于新局。",
            "1.9.5" => "1.9.5（万籁三速·高目标试用）沿用1.9.4。首年调整黄帽额外充电估计，让符合条件的训练在满五时进入原两步比较。主动选择后仅用于新建。",
            "1.9.4" => "1.9.4（末训目标缺口，试用）：沿用1.9.3；仅在第二次远征最后自由训练且原采购比较选择不购买（空篮）时，按当前目标缺口调整训练选择。只在主动选择后用于新建。",
            "1.9.3" => "1.9.3（试用）：在受支持的正常适应点输入下，把全程采购与后续行动联合比较；采购仍由原流程执行和结算，沿用失败率上限与资源限制。",
            "1.9.2.1" => "1.9.2.1低智分支（试用）：保留原1.9.2父策略，首次远征按短路线联合比较训练、恢复与采购。",
            "1.9.2" => "1.9.2（试用）：继承1.8.5上限分支，首次远征按原有估值比较Pt与训练升级，首次回国前结合后续训练机会判断是否休息。",
            "1.9" => "1.9（试用）：继承1.8.5上限分支，调整体力相关行动的比较顺序；新局默认速1、耐1、力1.25、毅力0.85、智1，达标后收益默认保留50%。",
            "1.8.5.1" => "1.8.5上限分支（试用）：采用上限取向的分支规则；新局默认速1、耐1、力1.25、毅力0.85、智1，达标后收益默认保留50%。",
            "1.8.5" => "1.8.5（试用）：沿用1.8.3 P25评分，并包含V3黄帽输入修复。",
            "1.8.4" => "1.8.4（试用）：以1.8.2为基础，五维默认均为1，达标后收益默认保留20%。",
            "1.8.3" => "1.8.3（试用）：继承1.8.2；新局默认速1、耐1、力1.25、毅力0.85、智1，侧重力量。",
            "1.8.2" => "1.8.2（试用）：继承1.8.1，在末次远征中一起考虑当前投资与凯旋门前升级预算。",
            "1.8.1" => "1.8.1（试用）：继承1.8，优化第二年国内训练取舍、SS衔接与未来智力训练估值。",
            "1.8" => "1.8（试用）：继承末海外训练与采购联合规划；保持当前失败率与资源设置。",
            "1.7.26.3" => "1.7.26.3（试用）：新局记录首次远征训练预览，支持25%训练失败率上限。",
            "1.7.16.1" => "1.7.16.1：训练评分沿用1.7.15；单项训练设施只在购买后为当日首选训练时支付。",
            "1.7.15" => " 首年前半黄帽在场+5／后半+3；SS、恢复与采购按阶段和当前资源规划。",
            "1.7" => " 黄帽当前可用且仍有次数时优先；结合剩余机会安排恢复与智力训练。",
            "1.6.1" => " 恢复1.5的目标内属性估值，仅在完整未来窗口不超过两步时使用历史布局推演。",
            "1.6" => " 按当前属性与本局已观测布局推演后续两步。",
            "1.5" => " 训练评分接近时结合体力取舍，避免重复计算恢复收益。",
            "1.4" => " 首次远征按实际配卡估计训练升级的未来收益。",
            "1.3" => " 1200以上、目标以下的显示增量按等效价值修正。",
            "1.2" or "1.1" => "保留所选旧版规则；可以显式切回其他已保存版本。",
            _ => null,
        };
        if (legacy != null)
        {
            notice = legacy;
            return true;
        }
        notice = "";
        return false;
    }

    public static double DefaultAfterTargetCredit(string? revision)
    {
        if (string.IsNullOrWhiteSpace(revision)) return 0.5;
        var entry = Find(revision);
        if (entry != null) return entry.AfterTargetCredit;
        return revision.Trim() == "1.8.4" ? 0.2 : 0.5;
    }

    public static string DefaultFailureLimit(string? revision)
    {
        var entry = Find(revision);
        return entry != null ? entry.DefaultFailureLimit : "25";
    }

    public static double DefaultWeight(string key, string? revision)
    {
        var entry = Find(revision);
        if (entry != null)
        {
            return key switch
            {
                "speed" => entry.SpeedWeight,
                "stamina" => entry.StaminaWeight,
                "power" => entry.PowerWeight,
                "guts" => entry.GutsWeight,
                "wisdom" => entry.WisdomWeight,
                _ => 1.0,
            };
        }
        if (TryParseRevisionTuple(revision, out var ver) && CompareVer(ver, (1, 8, 3)) >= 0)
        {
            return key switch { "power" => 1.25, "guts" => 0.85, _ => 1.0 };
        }
        return 1.0;
    }

    public static bool IsDefaultWeights(JObject? weights, string? revision)
    {
        if (weights == null || weights.Count == 0) return true;
        foreach (var prop in weights.Properties())
        {
            if (prop.Value is not JValue val || val.Type is not (JTokenType.Float or JTokenType.Integer))
                return false;
            var actual = val.Value<double>();
            var target = DefaultWeight(prop.Name, revision);
            if (Math.Abs(actual - target) > 1e-4) return false;
        }
        return true;
    }

    public static string FamilyOf(string? revision)
    {
        if (string.IsNullOrWhiteSpace(revision)) return "unknown";
        var entry = Find(revision);
        if (entry != null && !string.IsNullOrWhiteSpace(entry.Family))
            return entry.Family;
        if (TryParseRevisionTuple(revision, out var ver))
        {
            if (CompareVer(ver, (2, 3, int.MaxValue)) <= 0)
                return "<=2.3";
            return $"{ver.Major}.{ver.Minor}";
        }
        return "unknown";
    }

    public static bool UsesBFamilyPreferences(string? revision)
    {
        if (string.IsNullOrWhiteSpace(revision)) return false;
        return TryParseRevisionTuple(revision, out var ver) && CompareVer(ver, (1, 9, 7)) >= 0;
    }

    private static List<string> DiscoverManifestPaths()
    {
        var result = new List<string>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        void TryAddManifest(string path)
        {
            try
            {
                var full = Path.GetFullPath(path);
                if (File.Exists(full) && seen.Add(full))
                    result.Add(full);
            }
            catch
            {
            }
        }

        void TryAddPackRoot(string dir)
        {
            try
            {
                if (!Directory.Exists(dir)) return;
                TryAddManifest(Path.Combine(dir, "manifest.json"));
                foreach (var child in Directory.GetDirectories(dir).OrderBy(x => x, StringComparer.OrdinalIgnoreCase))
                    TryAddManifest(Path.Combine(child, "manifest.json"));
            }
            catch
            {
            }
        }

        var envRaw = Environment.GetEnvironmentVariable("MAAUMA_STRATEGY_PACK_DIR");
        if (string.Equals(envRaw, "__NONE__", StringComparison.Ordinal))
            return result;

        if (!string.IsNullOrWhiteSpace(envRaw))
        {
            foreach (var part in envRaw.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
                TryAddPackRoot(part);
        }

        var baseDir = AppContext.BaseDirectory;
        foreach (var rel in new[]
        {
            Path.Combine("pacer", "strategy_pack"),
            Path.Combine("pacer", "strategy_packs"),
            Path.Combine("pacer", "backend", "strategy_pack"),
            Path.Combine("pacer", "backend", "strategy_packs"),
            "strategy_pack",
            "strategy_packs",
        })
        {
            TryAddPackRoot(Path.Combine(baseDir, rel));
        }

        return result;
    }

    private static Snapshot BuildSnapshot()
    {
        var byRevision = new Dictionary<string, StrategyRevisionEntry>(StringComparer.Ordinal);
        foreach (var b in BaselineEntries)
            byRevision[b.Revision] = b;

        var packNames = new List<string>();
        var seenPackIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var unlockedModes = new HashSet<string>(StringComparer.Ordinal) { "direct_career" };
        var allowCustomModes = false;
        var weightPresets = new List<WeightPresetEntry>();
        var seenPresetIds = new HashSet<string>(StringComparer.Ordinal);
        var skillLadderPresets = new List<SkillLadderPresetEntry>();
        var seenLadderPresetIds = new HashSet<string>(StringComparer.Ordinal);
        string? packDefaultRevision = null;

        foreach (var manifestPath in DiscoverManifestPaths())
        {
            try
            {
                var obj = JObject.Parse(File.ReadAllText(manifestPath));
                if (!string.Equals((string?)obj["schema_version"], "maauma.strategy_pack.v1", StringComparison.Ordinal))
                    continue;
                var packDir = Path.GetDirectoryName(manifestPath) ?? AppContext.BaseDirectory;
                var packId = ((string?)obj["pack_id"])?.Trim();
                var packKey = !string.IsNullOrEmpty(packId) ? packId : Path.GetFullPath(manifestPath);
                if (!seenPackIds.Add(packKey))
                    continue;

                var packName = (string?)obj["display_name"] ?? packId ?? Path.GetFileName(packDir);
                packNames.Add(packName);

                var defRev = (string?)obj["default_revision"];
                if (!string.IsNullOrWhiteSpace(defRev))
                    packDefaultRevision = defRev.Trim();

                if (obj["unlock_all_workflow_modes"]?.Value<bool?>() == true)
                {
                    foreach (var mode in UmaWorkflowLibraryModel.BuiltinModes)
                        unlockedModes.Add(mode.Id);
                    allowCustomModes = true;
                }
                if (obj["allow_custom_workflows"]?.Value<bool?>() == true)
                    allowCustomModes = true;
                if (obj["workflow_modes"] is JArray modeList)
                {
                    foreach (var token in modeList)
                    {
                        var modeId = (token.Value<string>() ?? "").Trim();
                        if (!string.IsNullOrEmpty(modeId) && UmaWorkflowLibraryModel.IsBuiltinMode(modeId))
                            unlockedModes.Add(modeId);
                    }
                }

                if (obj["weight_presets"] is JArray presetList)
                {
                    var packRootFull = Path.GetFullPath(packDir).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    foreach (var pItem in presetList.OfType<JObject>())
                    {
                        var pid = ((string?)pItem["id"] ?? "").Trim();
                        var pname = ((string?)pItem["name"] ?? "").Trim();
                        if (string.IsNullOrEmpty(pid) || string.IsNullOrEmpty(pname) || seenPresetIds.Contains(pid))
                            continue;
                        string? profileJson = null;
                        if (pItem["file"] is JValue { Type: JTokenType.String } fileTok)
                        {
                            var rel = (fileTok.Value<string>() ?? "").Trim();
                            if (!string.IsNullOrEmpty(rel))
                            {
                                var targetFull = Path.GetFullPath(Path.Combine(packDir, rel));
                                if (targetFull.StartsWith(packRootFull, StringComparison.OrdinalIgnoreCase) && File.Exists(targetFull))
                                {
                                    var parsedProfile = JObject.Parse(File.ReadAllText(targetFull));
                                    profileJson = parsedProfile.ToString(Newtonsoft.Json.Formatting.None);
                                }
                            }
                        }
                        else if (pItem["profile"] is JObject inlineProfile)
                        {
                            profileJson = inlineProfile.ToString(Newtonsoft.Json.Formatting.None);
                        }
                        if (string.IsNullOrWhiteSpace(profileJson))
                            continue;
                        seenPresetIds.Add(pid);
                        weightPresets.Add(new WeightPresetEntry(
                            pid,
                            pname,
                            ((string?)pItem["summary"] ?? "").Trim(),
                            string.IsNullOrWhiteSpace((string?)pItem["recommended_revision"]) ? null : ((string?)pItem["recommended_revision"])!.Trim(),
                            profileJson,
                            packId ?? packKey
                        ));
                    }
                }

                if (obj["skill_ladder_presets"] is JArray ladderList)
                {
                    var packRootFull = Path.GetFullPath(packDir).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    foreach (var lItem in ladderList.OfType<JObject>())
                    {
                        var lid = ((string?)lItem["id"] ?? "").Trim();
                        var lname = ((string?)lItem["name"] ?? "").Trim();
                        if (string.IsNullOrEmpty(lid) || string.IsNullOrEmpty(lname) || seenLadderPresetIds.Contains(lid))
                            continue;
                        string? policyJson = null;
                        if (lItem["file"] is JValue { Type: JTokenType.String } lFileTok)
                        {
                            var rel = (lFileTok.Value<string>() ?? "").Trim();
                            if (!string.IsNullOrEmpty(rel))
                            {
                                var targetFull = Path.GetFullPath(Path.Combine(packDir, rel));
                                if (targetFull.StartsWith(packRootFull, StringComparison.OrdinalIgnoreCase) && File.Exists(targetFull))
                                {
                                    var parsedPolicy = JObject.Parse(File.ReadAllText(targetFull));
                                    policyJson = parsedPolicy.ToString(Newtonsoft.Json.Formatting.None);
                                }
                            }
                        }
                        else if (lItem["policy"] is JObject inlinePolicy)
                        {
                            policyJson = inlinePolicy.ToString(Newtonsoft.Json.Formatting.None);
                        }
                        if (string.IsNullOrWhiteSpace(policyJson))
                            continue;
                        seenLadderPresetIds.Add(lid);
                        skillLadderPresets.Add(new SkillLadderPresetEntry(
                            lid,
                            lname,
                            ((string?)lItem["summary"] ?? "").Trim(),
                            policyJson,
                            packId ?? packKey
                        ));
                    }
                }

                if (obj["ui_revisions"] is JArray uiList)
                {
                    foreach (var item in uiList.OfType<JObject>())
                    {
                        var rev = ((string?)item["revision"] ?? "").Trim();
                        if (string.IsNullOrEmpty(rev)) continue;
                        var label = (string?)item["label"] ?? rev;
                        var notice = (string?)item["notice"] ?? "";
                        var limitToken = item["default_failure_limit"];
                        var limitStr = limitToken != null
                            ? Convert.ToInt32(Math.Round(limitToken.Value<double>()), CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture)
                            : "25";
                        var credit = item["after_target_credit"]?.Value<double?>() ?? 0.5;
                        var wObj = item["attribute_weights"] as JObject;
                        double ReadW(string k, double fallback) => wObj?[k]?.Value<double?>() ?? fallback;
                        var autoRouted = item["auto_routed"]?.Value<bool?>() ?? false;
                        var family = (string?)item["family"];
                        if (string.IsNullOrWhiteSpace(family))
                            family = TryParseRevisionTuple(rev, out var vt) ? $"{vt.Major}.{vt.Minor}" : "external";
                        var aliases = (item["aliases"] as JArray)?
                            .Select(t => t.Value<string>())
                            .Where(s => !string.IsNullOrWhiteSpace(s))
                            .Select(s => s!.Trim())
                            .ToArray() ?? Array.Empty<string>();

                        byRevision[rev] = new StrategyRevisionEntry(
                            rev,
                            label,
                            notice,
                            limitStr,
                            credit,
                            ReadW("speed", 1.0),
                            ReadW("stamina", 1.0),
                            ReadW("power", 1.25),
                            ReadW("guts", 0.85),
                            ReadW("wisdom", 1.0),
                            family!,
                            autoRouted,
                            true,
                            aliases
                        );
                    }
                }
            }
            catch
            {
            }
        }

        var ordered = byRevision.Values
            .OrderByDescending(entry => RevisionSortKey(entry.Revision, entry.AutoRouted))
            .ThenByDescending(entry => entry.Revision, StringComparer.Ordinal)
            .ToArray();

        var orderedRevs = ordered.Select(e => e.Revision).ToArray();
        var lookup = new Dictionary<string, StrategyRevisionEntry>(StringComparer.Ordinal);
        foreach (var entry in ordered)
        {
            lookup[entry.Revision] = entry;
            foreach (var alias in entry.Aliases)
                lookup.TryAdd(alias, entry);
        }

        var effectiveDefault = !string.IsNullOrWhiteSpace(packDefaultRevision) && lookup.ContainsKey(packDefaultRevision)
            ? packDefaultRevision
            : BaselineDefaultRevision;

        var orderedUnlockedModeIds = UmaWorkflowLibraryModel.BuiltinModes
            .Select(m => m.Id)
            .Where(id => unlockedModes.Contains(id))
            .ToArray();
        var summary = packNames.Count > 0 ? string.Join(" + ", packNames) : "";
        return new Snapshot(
            packNames.Count > 0,
            summary,
            effectiveDefault,
            ordered,
            orderedRevs,
            lookup,
            orderedUnlockedModeIds,
            unlockedModes,
            allowCustomModes,
            weightPresets.ToArray(),
            skillLadderPresets.ToArray()
        );
    }

    private static (int Major, int Minor, int Build, int Revision) RevisionSortKey(string revision, bool autoRouted)
    {
        if (!TryParseVersionObject(revision, out var v))
            return (0, 0, -1, -1);
        var build = autoRouted && v.Build < 0 ? int.MaxValue : v.Build;
        return (v.Major, v.Minor, build, v.Revision);
    }

    private static bool TryParseVersionObject(string? revision, out Version version)
    {
        version = new Version(0, 0);
        if (string.IsNullOrWhiteSpace(revision)) return false;
        var clean = revision.Trim();
        var cut = clean.IndexOfAny(new[] { '_', '-', '+' });
        if (cut > 0) clean = clean[..cut];
        if (Version.TryParse(clean, out var parsed) && parsed != null)
        {
            version = parsed;
            return true;
        }
        return false;
    }

    private static bool TryParseRevisionTuple(string? revision, out (int Major, int Minor, int Build) ver)
    {
        ver = default;
        if (!TryParseVersionObject(revision, out var parsed)) return false;
        ver = (parsed.Major, parsed.Minor, parsed.Build < 0 ? 0 : parsed.Build);
        return true;
    }

    private static int CompareVer((int Major, int Minor, int Build) a, (int Major, int Minor, int Build) b)
    {
        var c = a.Major.CompareTo(b.Major);
        if (c != 0) return c;
        c = a.Minor.CompareTo(b.Minor);
        if (c != 0) return c;
        return a.Build.CompareTo(b.Build);
    }
}
