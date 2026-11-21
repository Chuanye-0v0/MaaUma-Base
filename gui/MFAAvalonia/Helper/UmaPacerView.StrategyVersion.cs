using System;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaPacerView
{
    internal static string DefaultStrategyRevision => StrategyPackCatalog.DefaultRevision;
    internal static string[] StrategyRevisions => StrategyPackCatalog.Revisions;
    internal static string[] StrategyItems(string retainLabel) => StrategyPackCatalog.StrategyItems(retainLabel);
    internal static string[] StrategyRevisionItems => StrategyItems("沿用已保存策略（历史设置）");
    internal static int FactoryStrategyIndex => StrategyPackCatalog.FactoryStrategyIndex;
    private JObject strategyOriginalSettings = new();
    private bool strategyRevisionEdited, newStrategyForm = true;
    private bool strategyFactoryPreferences;
    private string SelectedStrategyRevision
    {
        get
        {
            var revisions = StrategyRevisions;
            return strategy.SelectedIndex >= 1 && strategy.SelectedIndex <= revisions.Length
                ? revisions[strategy.SelectedIndex - 1]
                : throw new InvalidOperationException("请明确选择策略版本；已保存的历史设置可以继续沿用。");
        }
    }
    private string? StrategyPreferenceRevision => strategy.SelectedIndex > 0
        ? SelectedStrategyRevision : (string?)loadedLarcPolicy?["revision"];

    private void StrategyRevisionNotice()
    {
        if (strategy.SelectedIndex == 0) { strategyStatus.Text = "沿用已保存策略、旧无revision设置及原版本语义，不自动迁移；继续原局使用开局记录。"; return; }
        if (strategy.SelectedIndex < 0) { strategyStatus.Text = "原策略设置保留，请选择沿用历史设置或明确版本。"; return; }
        var chosen = SelectedStrategyRevision;
        var detail = StrategyPackCatalog.TryGetKnownNotice(chosen, out var notice) ? notice : " 保留所选旧版规则。";
        strategyStatus.Text = $"下次新建使用策略 {chosen}；继续原局沿用开局记录。" + detail + " 不会自动开启策略画布。";
    }

    private void LoadStrategyRevision(JObject settings)
    {
        strategyOriginalSettings = (JObject)settings.DeepClone(); strategyRevisionEdited = false; newStrategyForm = settings.Count == 0;
        var revision = loadedLarcPolicy?["revision"]; var baseline = (string?)loadedLarcPolicy?["variant"] == "baseline";
        var index = baseline && revision?.Type == JTokenType.String ? Array.IndexOf(StrategyRevisions, (string)revision) : -1;
        strategy.SelectedIndex = newStrategyForm ? FactoryStrategyIndex : index >= 0 ? index + 1 : 0; StrategyRevisionNotice();
    }

    internal static double DefaultAfterTargetCreditForRevision(string? revision) =>
        StrategyPackCatalog.DefaultAfterTargetCredit(revision);

    internal static string DefaultFailureLimitForRevision(string? revision) =>
        StrategyPackCatalog.DefaultFailureLimit(revision);

    internal static bool IsDefaultWeightsForRevision(JObject? weights, string? revision) =>
        StrategyPackCatalog.IsDefaultWeights(weights, revision);

    private JObject StrategyPolicyForNextRun()
    {
        var policy = loadedLarcPolicy?.DeepClone() as JObject ?? new JObject();
        var oldRev = (string?)policy["revision"];
        return PacerPurity.AdoptRevision(policy, oldRev, SelectedStrategyRevision);
    }
    private JObject ApplyStrategySelection(JObject options)
    {
        if (newStrategyForm || strategyRevisionEdited) { options["strategy"] = "baseline"; options["larc_policy"] = StrategyPolicyForNextRun(); return options; }
        foreach (var key in new[] { "strategy", "larc_policy" })
        {
            if (strategyOriginalSettings.Property(key) != null) options[key] = strategyOriginalSettings[key]!.DeepClone(); else options.Remove(key);
        }
        if (IsCut && strategyOriginalSettings["cut"] is JObject oldCut && oldCut.Property("larc_policy") != null && options["cut"] is JObject nextCut)
            nextCut["larc_policy"] = oldCut["larc_policy"]!.DeepClone();
        return options;
    }

    private void ConnectStrategyRevisionSelector()
    {
        strategy.SelectionChanged += (_, _) =>
        {
            if (applying) return;
            if (newStrategyForm && strategy.SelectedIndex == 0) { var previous = applying; applying = true; strategy.SelectedIndex = FactoryStrategyIndex; applying = previous; }
            strategyRevisionEdited = strategy.SelectedIndex > 0;
            if (strategy.SelectedIndex > 0 && risk.Text?.Trim() is "25" or "28")
                risk.Text = DefaultFailureLimitForRevision(SelectedStrategyRevision);
            decisionPreferences.SetStrategyContext(StrategyPreferenceRevision, factoryDefaults: strategyFactoryPreferences);
            StrategyRevisionNotice(); cutDraft = Options(); if (standaloneCutProfiles) savePreset.Content = "保存配置";
        };
        decisionPreferences.SetStrategyContext(StrategyPreferenceRevision);
        StrategyRevisionNotice();
    }
}
