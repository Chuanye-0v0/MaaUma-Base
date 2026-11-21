using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Edits skill_timing.arc_policy for Package 1 (10-track CM skill auto-compiler & terminal spenddown).</summary>
public sealed class UmaArcSkillPolicyEditor : StackPanel
{
    private sealed record PresetItem(string? Id, string Label, string? DistanceType);
    private sealed record ChoiceItem(string? Key, string Label);

    private static readonly PresetItem[] TrackPresets =
    {
        new(null, "自动识别 / 按距离近似", null),
        new("longchamp_2400_turf", "隆尚 2400m · 草地（凯旋门 / 中距离）", "middle"),
        new("tokyo_2400_turf", "东京 2400m · 草地（日本德比 / 橡树）", "middle"),
        new("tokyo_1600_turf", "东京 1600m · 草地（安田 / 维多利亚英里）", "mile"),
        new("nakayama_2000_turf", "中山 2000m · 草地（皋月赏 / 紫苑）", "middle"),
        new("nakayama_2500_turf", "中山 2500m · 草地（有马纪念 / 长距离）", "long"),
        new("hanshin_2200_turf", "阪神 2200m · 草地（宝冢纪念）", "middle"),
        new("kyoto_3000_turf", "京都 3000m · 草地（菊花赏）", "long"),
        new("kyoto_3200_turf", "京都 3200m · 草地（天皇赏春）", "long"),
        new("chukyo_1200_turf", "中京 1200m · 草地（高松宫 / 短距离）", "short"),
        new("ooi_2000_dirt", "大井 2000m · 泥地（帝王赏 / 东京大赏典）", "middle"),
    };

    private static readonly ChoiceItem[] StyleChoices =
    {
        new(null, "不指定跑法（通用）"),
        new("leader", "领跑 (逃马 / leader)"),
        new("front", "跟前 (先行 / front)"),
        new("betweener", "居中 (差马 / betweener)"),
        new("chaser", "后追 (追马 / chaser)"),
    };

    private static readonly ChoiceItem[] Distances =
    {
        new(null, "不指定 / 自动"),
        new("short", "短距离 (<1600m)"),
        new("mile", "英里 (1600~1800m)"),
        new("middle", "中距离 (2000~2400m)"),
        new("long", "长距离 (>2400m)"),
    };

    private static readonly string[] PresetDerivedTargetKeys =
    {
        "track_id", "distance_m", "surface", "rotation", "ground_condition", "season",
    };

    private readonly CheckBox enabled = new() { Name = "ArcPolicyEnabled", Content = "启用凯旋门赛前与终盘目标选技" };
    private readonly CheckBox autoCm = new()
    {
        Name = "ArcPolicyAutoCm",
        Content = "启用 CM 大赛智能编译与终局清空余点（Ver3.4 几何窗口 + 回体门控 + 地固联动）",
        IsChecked = true,
    };
    private readonly ComboBox presetPicker = new()
    {
        Name = "ArcPolicyPresetId",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = TrackPresets.Select(p => p.Label).ToArray(),
        SelectedIndex = 0,
    };
    private readonly ComboBox stylePicker = new()
    {
        Name = "ArcPolicyStyle",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = StyleChoices.Select(s => s.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 175,
    };
    private readonly ComboBox distancePicker = new()
    {
        Name = "ArcPolicyDistanceType",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = Distances.Select(d => d.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 165,
    };
    private readonly NumericUpDown midReserve = new()
    {
        Name = "ArcPolicyMidReservePt",
        Minimum = 0,
        Maximum = 2000,
        Increment = 20,
        FormatString = "0",
        Value = 360,
        MinWidth = 140,
    };
    private readonly NumericUpDown minMidDiscount = new()
    {
        Name = "ArcPolicyMinMidDiscount",
        Minimum = 0,
        Maximum = 60,
        Increment = 5,
        FormatString = "0",
        Value = 20,
        MinWidth = 140,
    };
    private readonly StackPanel details = new() { Spacing = 8 };
    private readonly TextBlock status = Note("");

    private bool loading;
    private JObject loaded = new();
    private string? loadedPresetId;

    public event Action? Changed;

    public string Summary
    {
        get
        {
            if (enabled.IsChecked != true)
                return "CM 目标选技：未启用";
            if (autoCm.IsChecked != true)
                return "目标选技：仅用手动经典年白名单";
            var preset = SelectedPreset;
            var track = preset.Id == null ? SelectedDistance.Label.Split(' ')[0] : preset.Label.Split('·')[0].Trim();
            var style = SelectedStyle.Key == null ? "通用跑法" : SelectedStyle.Label.Split(' ')[0];
            return $"CM 智能选技：{track} · {style}";
        }
    }

    private PresetItem SelectedPreset =>
        presetPicker.SelectedIndex >= 0 && presetPicker.SelectedIndex < TrackPresets.Length
            ? TrackPresets[presetPicker.SelectedIndex]
            : TrackPresets[0];

    private ChoiceItem SelectedStyle =>
        stylePicker.SelectedIndex >= 0 && stylePicker.SelectedIndex < StyleChoices.Length
            ? StyleChoices[stylePicker.SelectedIndex]
            : StyleChoices[0];

    private ChoiceItem SelectedDistance =>
        distancePicker.SelectedIndex >= 0 && distancePicker.SelectedIndex < Distances.Length
            ? Distances[distancePicker.SelectedIndex]
            : Distances[0];

    public UmaArcSkillPolicyEditor()
    {
        Name = "PacerArcSkillPolicy";
        Spacing = 8;
        Children.Add(enabled);
        Children.Add(Note("针对目标大赛赛道编译 T0/T1/T2 优先级、Ver3.4 2101m+ 耐力回体阈值及地固前置绑定；终局自动清空安全余点。"));

        details.Children.Add(autoCm);
        details.Children.Add(Field("目标 CM 赛道预设", presetPicker));
        var row = new WrapPanel();
        row.Children.Add(Field("目标跑法", stylePicker));
        row.Children.Add(Field("近似距离（未选预设时使用）", distancePicker));
        details.Children.Add(row);

        var budgets = new WrapPanel();
        budgets.Children.Add(Field("经典年赛前保留 Pt（默认 360）", midReserve));
        budgets.Children.Add(Field("经典年最低折扣（% · 默认 20）", minMidDiscount));
        details.Children.Add(budgets);
        Children.Add(details);
        Children.Add(status);

        enabled.IsCheckedChanged += (_, _) => Notify();
        autoCm.IsCheckedChanged += (_, _) => Notify();
        presetPicker.SelectionChanged += (_, _) =>
        {
            if (!loading) SyncPresetDistanceState();
            Notify();
        };
        stylePicker.SelectionChanged += (_, _) => Notify();
        distancePicker.SelectionChanged += (_, _) => Notify();
        midReserve.ValueChanged += (_, _) => Notify();
        minMidDiscount.ValueChanged += (_, _) => Notify();

        RefreshUiState();
    }

    private static TextBlock Note(string text) => new() { Text = text, FontSize = 12, TextWrapping = TextWrapping.Wrap };

    private static Control Field(string label, Control field) =>
        new StackPanel { Spacing = 4, Margin = new Thickness(0, 0, 12, 6), Children = { Note(label), field } };

    private void SyncPresetDistanceState()
    {
        var preset = SelectedPreset;
        if (preset.Id != null)
        {
            var idx = Array.FindIndex(Distances, d => d.Key == preset.DistanceType);
            if (idx >= 0) distancePicker.SelectedIndex = idx;
        }
        distancePicker.IsEnabled = enabled.IsChecked == true && autoCm.IsChecked == true && preset.Id == null;
    }

    private void RefreshUiState()
    {
        var on = enabled.IsChecked == true;
        var cmOn = autoCm.IsChecked == true;
        details.IsEnabled = on;
        presetPicker.IsEnabled = on && cmOn;
        stylePicker.IsEnabled = on && cmOn;
        midReserve.IsEnabled = on;
        minMidDiscount.IsEnabled = on;
        SyncPresetDistanceState();
        status.Text = Summary;
    }

    private void Notify()
    {
        if (loading) return;
        RefreshUiState();
        Changed?.Invoke();
    }

    public void Load(JToken? value)
    {
        loading = true;
        try
        {
            var obj = value as JObject;
            loaded = obj == null ? new JObject() : (JObject)obj.DeepClone();
            enabled.IsChecked = (bool?)obj?["enabled"] == true;
            autoCm.IsChecked = (bool?)obj?["auto_cm"] != false;
            loadedPresetId = (string?)obj?["preset_id"];
            var presetIdx = Array.FindIndex(TrackPresets, p => string.Equals(p.Id, loadedPresetId, StringComparison.Ordinal));
            presetPicker.SelectedIndex = presetIdx >= 0 ? presetIdx : 0;

            var target = obj?["final_target"] as JObject;
            var style = (string?)target?["style"];
            var styleIdx = Array.FindIndex(StyleChoices, s => string.Equals(s.Key, style, StringComparison.Ordinal));
            stylePicker.SelectedIndex = styleIdx >= 0 ? styleIdx : 0;

            var dist = (string?)target?["distance_type"];
            if (SelectedPreset.Id != null && SelectedPreset.DistanceType != null)
                dist = SelectedPreset.DistanceType;
            var distIdx = Array.FindIndex(Distances, d => string.Equals(d.Key, dist, StringComparison.Ordinal));
            distancePicker.SelectedIndex = distIdx >= 0 ? distIdx : 0;

            midReserve.Value = Math.Clamp((int?)obj?["mid_reserve_pt"] ?? 360, 0, 2000);
            minMidDiscount.Value = Math.Clamp((int?)obj?["min_mid_discount_percent"] ?? 20, 0, 60);
            RefreshUiState();
        }
        finally
        {
            loading = false;
        }
    }

    public JObject ReadDraft() => ReadCore(validate: false);

    public JObject Read() => ReadCore(validate: true);

    private JObject ReadCore(bool validate)
    {
        var reserveDec = midReserve.Value ?? 360;
        var discountDec = minMidDiscount.Value ?? 20;
        if (validate && (reserveDec != decimal.Truncate(reserveDec) || reserveDec < 0 || reserveDec > 2000))
            throw new FormatException("经典年保留 Pt 请填 0～2000 的整数。");
        if (validate && (discountDec != decimal.Truncate(discountDec) || discountDec < 0 || discountDec > 60))
            throw new FormatException("经典年最低折扣请填 0～60 的整数。");

        var result = new JObject
        {
            ["enabled"] = enabled.IsChecked == true,
            ["auto_cm"] = autoCm.IsChecked == true,
        };

        var preset = SelectedPreset;
        if (preset.Id != null)
            result["preset_id"] = preset.Id;
        else
            result["preset_id"] = JValue.CreateNull();

        var existingTarget = loaded["final_target"] as JObject;
        var target = preset.Id != null && string.Equals(preset.Id, loadedPresetId, StringComparison.Ordinal) && existingTarget != null
            ? (JObject)existingTarget.DeepClone()
            : new JObject();
        if (!string.Equals(preset.Id, loadedPresetId, StringComparison.Ordinal))
        {
            foreach (var key in PresetDerivedTargetKeys)
                target.Remove(key);
        }

        var styleKey = SelectedStyle.Key;
        if (styleKey != null) target["style"] = styleKey;
        else target.Remove("style");

        if (preset.Id != null)
        {
            // Keep preset authoritative and avoid any distance_m vs distance_type conflict.
            target.Remove("distance_type");
        }
        else
        {
            var distKey = SelectedDistance.Key;
            if (distKey != null) target["distance_type"] = distKey;
            else target.Remove("distance_type");
            foreach (var key in PresetDerivedTargetKeys)
                if (existingTarget == null || loadedPresetId != null) target.Remove(key);
        }

        result["final_target"] = target;
        result["classic_allowlist"] = loaded["classic_allowlist"] is JArray list
            ? (JArray)list.DeepClone()
            : new JArray();
        result["mid_reserve_pt"] = Math.Clamp((int)decimal.Truncate(reserveDec), 0, 2000);
        result["min_mid_discount_percent"] = Math.Clamp((int)decimal.Truncate(discountDec), 0, 60);
        return result;
    }
}
