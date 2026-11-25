using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Shared native editor for multi-node continuation, luck gating, and preset kill-lines.</summary>
public sealed class UmaContinuationOptions : StackPanel
{
    public bool EnabledForRun => enabled.IsChecked == true;
    public bool HasUnsubmittedDraft { get; private set; }
    public string DraftNotice => HasUnsubmittedDraft ? validation.Text ?? "" : "";
    public event Action? EnabledChanged;
    public event Action<JObject?>? TemplateApplied;

    private bool _allowRestart = true;
    private bool _aceCycle;

    private readonly CheckBox enabled = new() { Content = "启用多节点条件筛选 / 胡度监控" };
    private readonly TextBlock validation = new() { Name = "PacerContinuationValidation", TextWrapping = TextWrapping.Wrap };
    private readonly ComboBox templateChoice = new() { HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly Button applyTemplateBtn = new() { Content = "套用模板" };
    private readonly TextBlock templateSummary = Note("");
    private readonly List<(string Label, string Summary, string ConfigJson)> templateEntries = new();

    private readonly ConditionSectionEditor juniorSection;
    private readonly ConditionSectionEditor preExpeditionSection;
    private readonly ConditionSectionEditor inheritance2Conditions;

    private readonly ComboBox inheritanceMode = Choice("筛选", "仅记录，不筛选");
    private readonly TextBox skills = new()
    {
        AcceptsReturn = true,
        MinHeight = 72,
        TextWrapping = TextWrapping.Wrap,
        Watermark = "目标技能：一行一项，可填国服名称或技能ID",
    };
    private readonly TextBox skillMinimum = Input("1", "技能最低命中数");
    private readonly CheckBox distanceEnabled = new() { Content = "启用目标距离路线（与技能路线二选一）" };
    private readonly ComboBox distance = Choice("短距离", "英里", "中距离", "长距离");
    private readonly ComboBox rank = Choice("G", "F", "E", "D", "C", "B", "A", "S");
    private readonly ComboBox inheritanceFailure = Choice("暂停保留并通知", "结束本局后回刷切");

    private readonly ComboBox inh2Mode = Choice("筛选", "仅记录，不筛选");
    private readonly TextBox inh2Skills = new()
    {
        AcceptsReturn = true,
        MinHeight = 64,
        TextWrapping = TextWrapping.Wrap,
        Watermark = "可选：二继承目标技能（一行一项）；留空则仅按上方条件组合判断",
    };
    private readonly TextBox inh2SkillMinimum = Input("1", "技能最低命中数");
    private readonly CheckBox inh2DistanceEnabled = new() { Content = "启用二继承目标距离路线（可选）" };
    private readonly ComboBox inh2Distance = Choice("短距离", "英里", "中距离", "长距离");
    private readonly ComboBox inh2Rank = Choice("G", "F", "E", "D", "C", "B", "A", "S");
    private readonly ComboBox inh2Failure = Choice("暂停保留并通知", "结束本局后回刷切");

    private readonly StackPanel customDatesPanel = new() { Spacing = 8 };
    private readonly List<CustomDateSection> customDateSections = new();
    private readonly ComboBox customYear = Choice("初级（第1年）", "经典（第2年）", "高级（第3年）");
    private readonly ComboBox customMonth = new() { ItemsSource = Enumerable.Range(1, 12).Select(m => $"{m}月").ToArray(), SelectedIndex = 11, MinWidth = 76 };
    private readonly ComboBox customHalf = Choice("上半月", "下半月");

    private readonly CheckBox notify = new() { Content = "使用已有通知设置提醒号主", IsChecked = true };

    public UmaContinuationOptions()
    {
        Spacing = 10;
        enabled.IsCheckedChanged += (_, _) => EnabledChanged?.Invoke();
        Children.Add(enabled);
        Children.Add(validation);
        Children.Add(BuildTemplateBar());
        Children.Add(Note("门槛由你定义；支持首年12月下、经典4月继承、经典6月远征前、高级4月二继承及任意自定义年月节点。普通育成模式下仅暂停保留并通知。"));

        juniorSection = new ConditionSectionEditor("条件", defaultFilterIndex: 1, showModeAndFail: true, sectionLabel: "首年");
        Children.Add(Section("第一节点 · 初级12月下", juniorSection));

        Children.Add(Section("第二节点 · 经典4月（首次继承后）",
            Labelled("节点模式", inheritanceMode),
            skills,
            Labelled("技能最低命中数", skillMinimum),
            Note("已学和已可学习都算，不购买；满足技能数量或指定距离等级任一条即继续。"),
            distanceEnabled, Labelled("目标距离", distance), Labelled("要求等级", rank),
            Labelled("明确未通过时", inheritanceFailure)));

        preExpeditionSection = new ConditionSectionEditor("条件", defaultFilterIndex: 1, showModeAndFail: true, sectionLabel: "首次远征前");
        Children.Add(Section("第三节点 · 经典6月下（首次远征前）", preExpeditionSection));

        inheritance2Conditions = new ConditionSectionEditor("二继承条件", defaultFilterIndex: 1, showModeAndFail: false, sectionLabel: "第二次继承后");
        Children.Add(Section("第四节点 · 高级4月（二次继承后）",
            Labelled("节点模式", inh2Mode),
            inheritance2Conditions,
            inh2Skills,
            Labelled("二继承技能最低命中数", inh2SkillMinimum),
            inh2DistanceEnabled, Labelled("二继承目标距离", inh2Distance), Labelled("要求等级", inh2Rank),
            Labelled("明确未通过时", inh2Failure)));

        Children.Add(Section("自定义日期筛选节点（支持经典12月下 / 高级6月下等任意时间）", BuildCustomDatesContent()));
        Children.Add(notify);
        Children.Add(Note("胡度值门槛与多节点条件均按当手实际观测判定；未知状态不会误杀。自动重开仅在支持自动重开的循环模式下可用。"));

        rank.SelectedIndex = 7;
        inh2Rank.SelectedIndex = 7;
        inheritanceMode.SelectedIndex = 1;
        inh2Mode.SelectedIndex = 1;
        customYear.SelectedIndex = 1;
        customHalf.SelectedIndex = 1;

        RefreshTemplateChoices();
    }

    private Control BuildTemplateBar()
    {
        var panel = new StackPanel { Spacing = 6 };
        var row = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("*,Auto"),
            ColumnSpacing = 8,
        };
        Grid.SetColumn(templateChoice, 0);
        Grid.SetColumn(applyTemplateBtn, 1);
        row.Children.Add(templateChoice);
        row.Children.Add(applyTemplateBtn);

        templateChoice.SelectionChanged += (_, _) =>
        {
            var idx = templateChoice.SelectedIndex;
            templateSummary.Text = idx >= 0 && idx < templateEntries.Count ? templateEntries[idx].Summary : "";
        };
        applyTemplateBtn.Click += (_, _) => ApplySelectedTemplate();

        panel.Children.Add(Note("筛选 / 杀马预设模板（可一键导入后继续微调）"));
        panel.Children.Add(row);
        panel.Children.Add(templateSummary);
        return panel;
    }

    public void RefreshTemplateChoices()
    {
        templateEntries.Clear();
        foreach (var preset in StrategyPackCatalog.ScreeningPresets)
            templateEntries.Add((preset.Name, preset.Summary, preset.ConfigJson));
        templateEntries.Add((
            "免费测试样本 · 首年+远征仅记录监控（暂停版）",
            "开放给所有用户的温和观察样本：自动记录首年与远征前五维和胡度值，未达标仅暂停不重开。",
            """
            {
              "schema_version": "larc_continuation.v1",
              "enabled": true,
              "notify": true,
              "junior": { "mode": "observe", "combine": "all", "min_pass": 1, "conditions": [ { "kind": "stat_sum_min", "minimum": 1600 } ], "on_fail": "pause" },
              "pre_expedition": { "mode": "observe", "combine": "all", "min_pass": 1, "conditions": [ { "kind": "realtime_luck_min", "minimum": -4 }, { "kind": "stat_sum_min", "minimum": 2400 } ], "on_fail": "pause" },
              "inheritance": { "mode": "observe", "skills": [], "min_skills": 1, "distances": {}, "on_fail": "pause" },
              "inheritance2": { "mode": "observe", "combine": "all", "min_pass": 1, "conditions": [], "skills": [], "min_skills": 1, "distances": {}, "on_fail": "pause" },
              "custom_dates": []
            }
            """));
        templateChoice.ItemsSource = templateEntries.Select(e => e.Label).ToArray();
        if (templateEntries.Count > 0)
            templateChoice.SelectedIndex = 0;
    }

    private void ApplySelectedTemplate()
    {
        var idx = templateChoice.SelectedIndex;
        if (idx < 0 || idx >= templateEntries.Count)
            return;
        try
        {
            var raw = JObject.Parse(templateEntries[idx].ConfigJson);
            var tf = raw["training_failure_restart"] as JObject;
            var clean = (JObject)raw.DeepClone();
            clean.Remove("training_failure_restart");
            Load(clean);
            SetMode(_allowRestart, _aceCycle);
            TemplateApplied?.Invoke(tf != null ? (JObject)tf.DeepClone() : null);
            EnabledChanged?.Invoke();
        }
        catch (Exception ex)
        {
            validation.Text = "套用筛选模板失败：" + ex.Message;
        }
    }

    private Control BuildCustomDatesContent()
    {
        var wrap = new StackPanel { Spacing = 8 };
        var quickRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var btnClassicDec = new Button { Content = "+ 经典12月下" };
        btnClassicDec.Click += (_, _) => AddCustomDate(2, 12, "second", "经典12月下");
        var btnSeniorJun = new Button { Content = "+ 高级6月下" };
        btnSeniorJun.Click += (_, _) => AddCustomDate(3, 6, "second", "高级6月下");
        quickRow.Children.Add(btnClassicDec);
        quickRow.Children.Add(btnSeniorJun);

        var addRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        var addBtn = new Button { Content = "+ 添加日期节点" };
        addBtn.Click += (_, _) =>
        {
            var y = customYear.SelectedIndex + 1;
            var m = customMonth.SelectedIndex + 1;
            var h = customHalf.SelectedIndex == 0 ? "first" : "second";
            var yName = y == 1 ? "初级" : y == 2 ? "经典" : "高级";
            AddCustomDate(y, m, h, $"{yName}{m}月{(h == "second" ? "下" : "上")}");
        };
        addRow.Children.Add(customYear);
        addRow.Children.Add(customMonth);
        addRow.Children.Add(customHalf);
        addRow.Children.Add(addBtn);

        wrap.Children.Add(quickRow);
        wrap.Children.Add(addRow);
        wrap.Children.Add(customDatesPanel);
        return wrap;
    }

    private void AddCustomDate(int year, int month, string half, string label, JObject? payload = null)
    {
        var id = (string?)payload?["id"];
        if (string.IsNullOrWhiteSpace(id))
            id = $"date_{year}_{month}_{half}";
        if (customDateSections.Any(s => string.Equals(s.NodeId, id, StringComparison.Ordinal)))
        {
            validation.Text = $"自定义日期节点「{label}」已存在，无需重复添加。";
            return;
        }
        validation.Text = "";
        var item = payload != null
            ? (JObject)payload.DeepClone()
            : new JObject
            {
                ["id"] = id,
                ["label"] = label,
                ["year"] = year,
                ["month"] = month,
                ["half"] = half,
                ["mode"] = "filter",
                ["combine"] = "all",
                ["min_pass"] = 1,
                ["conditions"] = new JArray(),
                ["on_fail"] = _allowRestart ? "restart" : "pause",
            };
        var section = new CustomDateSection(item);
        section.SetMode(_allowRestart, _aceCycle);
        section.Removed += () =>
        {
            customDateSections.Remove(section);
            customDatesPanel.Children.Remove(section);
        };
        customDateSections.Add(section);
        customDatesPanel.Children.Add(section);
    }

    public JObject Read() => Read(true);
    public JObject ReadDraft() => Read(false);

    public bool TryValidateForSave(out JObject? validated)
    {
        if (!EnabledForRun)
        {
            HasUnsubmittedDraft = false;
            validation.ClearValue(TextBlock.ForegroundProperty);
            validation.Text = "";
            validated = ReadDraft();
            return true;
        }
        try
        {
            validated = Read(true);
            HasUnsubmittedDraft = false;
            validation.ClearValue(TextBlock.ForegroundProperty);
            validation.Text = "";
            return true;
        }
        catch (FormatException error)
        {
            HasUnsubmittedDraft = true;
            validation.Foreground = Brushes.IndianRed;
            validation.Text = error.Message;
            validated = null;
            return false;
        }
    }

    public JObject ReadForSubmission(JObject? saved)
    {
        try
        {
            var result = Read();
            HasUnsubmittedDraft = false;
            validation.ClearValue(TextBlock.ForegroundProperty);
            validation.Text = "";
            return result;
        }
        catch (FormatException error)
        {
            if (EnabledForRun)
            {
                HasUnsubmittedDraft = false;
                validation.Foreground = Brushes.IndianRed;
                validation.Text = "续养/筛选配置未完成：" + error.Message;
                throw;
            }
            HasUnsubmittedDraft = true;
            validation.ClearValue(TextBlock.ForegroundProperty);
            validation.Text = "未启用筛选：未完成的草稿仅保留在本窗口，不会提交；已保存的条件保留。";
            var result = saved == null
                ? new JObject { ["schema_version"] = "larc_continuation.v1" }
                : (JObject)saved.DeepClone();
            result["enabled"] = false;
            return result;
        }
    }

    private JObject Read(bool validate)
    {
        var distances = new JObject();
        if (distanceEnabled.IsChecked == true)
            distances[new[] { "short", "mile", "medium", "long" }[Math.Max(0, distance.SelectedIndex)]] = rank.SelectedItem?.ToString() ?? "S";

        var inh2Distances = new JObject();
        if (inh2DistanceEnabled.IsChecked == true)
            inh2Distances[new[] { "short", "mile", "medium", "long" }[Math.Max(0, inh2Distance.SelectedIndex)]] = inh2Rank.SelectedItem?.ToString() ?? "S";

        var inh2Obj = inheritance2Conditions.Read(validate, includeModeAndFail: false);
        inh2Obj["mode"] = inh2Mode.SelectedIndex == 0 ? "filter" : "observe";
        inh2Obj["skills"] = new JArray((inh2Skills.Text ?? "").Split('\n').Select(s => s.Trim()).Where(s => s.Length > 0).Distinct());
        inh2Obj["min_skills"] = Value(inh2SkillMinimum, validate, 1, "第二次继承后·技能最低命中数", 1, 200);
        inh2Obj["distances"] = inh2Distances;
        inh2Obj["on_fail"] = _allowRestart && inh2Failure.SelectedIndex == 1 ? "restart" : "pause";

        return new JObject
        {
            ["schema_version"] = "larc_continuation.v1",
            ["enabled"] = enabled.IsChecked == true,
            ["notify"] = notify.IsChecked == true,
            ["junior"] = juniorSection.Read(validate, includeModeAndFail: true),
            ["pre_expedition"] = preExpeditionSection.Read(validate, includeModeAndFail: true),
            ["inheritance"] = new JObject
            {
                ["mode"] = inheritanceMode.SelectedIndex == 0 ? "filter" : "observe",
                ["skills"] = new JArray((skills.Text ?? "").Split('\n').Select(s => s.Trim()).Where(s => s.Length > 0).Distinct()),
                ["min_skills"] = Value(skillMinimum, validate, 1, "继承后·技能最低命中数", 1, 200),
                ["distances"] = distances,
                ["on_fail"] = _allowRestart && inheritanceFailure.SelectedIndex == 1 ? "restart" : "pause",
            },
            ["inheritance2"] = inh2Obj,
            ["custom_dates"] = new JArray(customDateSections.Select(s => s.Read(validate))),
        };
    }

    public void Load(JObject? value)
    {
        HasUnsubmittedDraft = false;
        validation.ClearValue(TextBlock.ForegroundProperty);
        validation.Text = "";
        value ??= new JObject();
        enabled.IsChecked = (bool?)value["enabled"] == true;
        notify.IsChecked = (bool?)value["notify"] != false;

        juniorSection.Load(value["junior"] as JObject, defaultObserve: true);
        preExpeditionSection.Load(value["pre_expedition"] as JObject, defaultObserve: true);

        var after = value["inheritance"] as JObject ?? new();
        inheritanceMode.SelectedIndex = (string?)after["mode"] == "filter" ? 0 : 1;
        skills.Text = string.Join("\n", after["skills"]?.Values<string>() ?? Array.Empty<string>());
        skillMinimum.Text = after["min_skills"]?.ToString() ?? "1";
        inheritanceFailure.SelectedIndex = (string?)after["on_fail"] == "restart" ? 1 : 0;
        var pair = (after["distances"] as JObject)?.Properties().FirstOrDefault();
        distanceEnabled.IsChecked = pair != null;
        if (pair != null)
        {
            distance.SelectedIndex = Math.Max(0, Array.IndexOf(new[] { "short", "mile", "medium", "long" }, pair.Name));
            rank.SelectedIndex = Math.Max(0, Array.IndexOf(new[] { "G", "F", "E", "D", "C", "B", "A", "S" }, pair.Value.ToString()));
        }

        var inh2 = value["inheritance2"] as JObject ?? new();
        inh2Mode.SelectedIndex = (string?)inh2["mode"] == "filter" ? 0 : 1;
        inheritance2Conditions.Load(inh2, defaultObserve: true);
        inh2Skills.Text = string.Join("\n", inh2["skills"]?.Values<string>() ?? Array.Empty<string>());
        inh2SkillMinimum.Text = inh2["min_skills"]?.ToString() ?? "1";
        inh2Failure.SelectedIndex = (string?)inh2["on_fail"] == "restart" ? 1 : 0;
        var pair2 = (inh2["distances"] as JObject)?.Properties().FirstOrDefault();
        inh2DistanceEnabled.IsChecked = pair2 != null;
        if (pair2 != null)
        {
            inh2Distance.SelectedIndex = Math.Max(0, Array.IndexOf(new[] { "short", "mile", "medium", "long" }, pair2.Name));
            inh2Rank.SelectedIndex = Math.Max(0, Array.IndexOf(new[] { "G", "F", "E", "D", "C", "B", "A", "S" }, pair2.Value.ToString()));
        }

        customDateSections.Clear();
        customDatesPanel.Children.Clear();
        foreach (var entry in value["custom_dates"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>())
        {
            var y = ParseIntToken(entry["year"], 2, 1, 3);
            var m = ParseIntToken(entry["month"], 12, 1, 12);
            var h = (string?)entry["half"] == "first" ? "first" : "second";
            var label = (string?)entry["label"] ?? $"{(y == 1 ? "初级" : y == 2 ? "经典" : "高级")}{m}月{(h == "second" ? "下" : "上")}";
            AddCustomDate(y, m, h, label, entry);
        }
        SetMode(_allowRestart, _aceCycle);
    }

    public void SetMode(bool allowRestart, bool aceCycle = false)
    {
        _allowRestart = allowRestart;
        _aceCycle = aceCycle;
        var restartLabel = aceCycle ? "放弃本局并重开下一把" : "结束本局后回刷切";
        ApplyFailureChoices(inheritanceFailure, allowRestart, restartLabel);
        ApplyFailureChoices(inh2Failure, allowRestart, restartLabel);
        juniorSection.SetMode(allowRestart, restartLabel);
        preExpeditionSection.SetMode(allowRestart, restartLabel);
        foreach (var section in customDateSections)
            section.SetMode(allowRestart, aceCycle);
    }

    private static void ApplyFailureChoices(ComboBox box, bool allowRestart, string restartLabel)
    {
        var idx = box.SelectedIndex;
        box.ItemsSource = new[] { "暂停保留并通知", restartLabel };
        box.IsEnabled = allowRestart;
        box.SelectedIndex = allowRestart ? Math.Clamp(idx, 0, 1) : 0;
    }

    private static int ParseIntToken(JToken? token, int fallback, int min, int max)
    {
        if (token == null || token.Type == JTokenType.Null)
            return fallback;
        if (token.Type == JTokenType.Integer)
            return Math.Clamp((int)token, min, max);
        var text = token.ToString().Trim();
        return int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed)
            ? Math.Clamp(parsed, min, max)
            : fallback;
    }

    private static int Number(TextBox box, int fallback = 0, string fieldLabel = "续养门槛", int min = 0, int max = 1000000000)
    {
        var text = (box.Text ?? "").Trim();
        if (string.IsNullOrWhiteSpace(text))
        {
            if (fallback >= min && fallback <= max)
            {
                box.ClearValue(TemplatedControl.BorderBrushProperty);
                return fallback;
            }
            box.BorderBrush = Brushes.IndianRed;
            throw new FormatException($"续养配置：{fieldLabel}应为 {min}～{max} 的整数。");
        }
        if (int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n) && n >= min && n <= max)
        {
            box.ClearValue(TemplatedControl.BorderBrushProperty);
            return n;
        }
        box.BorderBrush = Brushes.IndianRed;
        throw new FormatException($"续养配置：{fieldLabel}应为 {min}～{max} 的整数。");
    }

    private static JToken Value(TextBox box, bool validate, int fallback = 0, string fieldLabel = "续养门槛", int min = 0, int max = 1000000000)
    {
        if (validate)
            return new JValue(Number(box, fallback, fieldLabel, min, max));
        var text = (box.Text ?? "").Trim();
        if (string.IsNullOrWhiteSpace(text))
            return new JValue("");
        return int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed)
            ? new JValue(parsed)
            : new JValue(text);
    }
    private static TextBox Input(string text, string hint) => new() { Text = text, Watermark = hint, MinWidth = 110 };
    private static ComboBox Choice(params string[] values) => new() { ItemsSource = values, SelectedIndex = 0, HorizontalAlignment = HorizontalAlignment.Stretch };
    private static TextBlock Note(string text) => new() { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = 12 };
    private static Control Labelled(string label, Control field) => new StackPanel { Spacing = 4, Children = { Note(label), field } };
    private static Expander Section(string label, params Control[] children)
    {
        var panel = new StackPanel { Spacing = 8 };
        foreach (var child in children)
            panel.Children.Add(child);
        return new Expander { Header = label, IsExpanded = false, Content = panel };
    }

    private sealed class CustomDateSection : Border
    {
        public string NodeId { get; }
        private readonly int year;
        private readonly int month;
        private readonly string half;
        private readonly string label;
        private readonly ConditionSectionEditor editor;
        public event Action? Removed;

        public CustomDateSection(JObject value)
        {
            NodeId = (string?)value["id"] ?? "date_2_12_second";
            year = ParseIntToken(value["year"], 2, 1, 3);
            month = ParseIntToken(value["month"], 12, 1, 12);
            half = (string?)value["half"] == "first" ? "first" : "second";
            label = (string?)value["label"] ?? $"{(year == 1 ? "初级" : year == 2 ? "经典" : "高级")}{month}月{(half == "second" ? "下" : "上")}";

            Padding = new Avalonia.Thickness(8);
            CornerRadius = new Avalonia.CornerRadius(4);
            BorderThickness = new Avalonia.Thickness(1);
            BorderBrush = Brushes.Gray;

            editor = new ConditionSectionEditor("日期条件", defaultFilterIndex: 0, showModeAndFail: true, sectionLabel: label);
            editor.Load(value, defaultObserve: false);

            var header = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 8 };
            var title = new TextBlock { Text = $"{label}（{NodeId}）", FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center };
            var removeBtn = new Button { Content = "删除此日期" };
            removeBtn.Click += (_, _) => Removed?.Invoke();
            Grid.SetColumn(title, 0);
            Grid.SetColumn(removeBtn, 1);
            header.Children.Add(title);
            header.Children.Add(removeBtn);

            Child = new StackPanel { Spacing = 6, Children = { header, editor } };
        }

        public void SetMode(bool allowRestart, bool aceCycle) =>
            editor.SetMode(allowRestart, aceCycle ? "放弃本局并重开下一把" : "结束本局后回刷切");

        public JObject Read(bool validate)
        {
            var obj = editor.Read(validate, includeModeAndFail: true);
            obj["id"] = NodeId;
            obj["label"] = label;
            obj["year"] = year;
            obj["month"] = month;
            obj["half"] = half;
            return obj;
        }
    }

    private sealed class ConditionSectionEditor : StackPanel
    {
        private readonly bool showModeAndFail;
        private readonly string sectionLabel;
        private readonly ComboBox mode = Choice("筛选", "仅记录，不筛选");
        private readonly ComboBox combine = Choice("已选条件全部达标", "至少 N 项达标");
        private readonly TextBox minimum = Input("1", "最低达标项数");
        private readonly StackPanel conditionPanel = new() { Spacing = 8 };
        private readonly List<ConditionRow> rows = new();
        private readonly ComboBox onFail = Choice("暂停保留并通知", "结束本局后回刷切");
        private bool allowRestartFlag = true;

        public ConditionSectionEditor(string addTargetName, int defaultFilterIndex, bool showModeAndFail, string sectionLabel = "节点")
        {
            this.showModeAndFail = showModeAndFail;
            this.sectionLabel = sectionLabel;
            Spacing = 8;
            if (showModeAndFail)
                Children.Add(Labelled("节点模式", mode));
            Children.Add(Labelled("条件组合", combine));
            Children.Add(Labelled("最低达标项数", minimum));
            Children.Add(conditionPanel);
            var addBtn = new Button { Content = $"添加{addTargetName}" };
            addBtn.Click += (_, _) => AddRow(new JObject { ["kind"] = "stat_sum_min" });
            Children.Add(addBtn);
            if (showModeAndFail)
                Children.Add(Labelled("明确未通过时", onFail));
            mode.SelectedIndex = defaultFilterIndex;
        }

        private void AddRow(JObject value)
        {
            var row = new ConditionRow(value);
            rows.Add(row);
            conditionPanel.Children.Add(row);
            row.Remove.Click += (_, _) =>
            {
                rows.Remove(row);
                conditionPanel.Children.Remove(row);
            };
        }

        public void Load(JObject? section, bool defaultObserve)
        {
            section ??= new JObject();
            mode.SelectedIndex = (string?)section["mode"] switch
            {
                "filter" => 0,
                "observe" => 1,
                _ => defaultObserve ? 1 : 0,
            };
            combine.SelectedIndex = (string?)section["combine"] == "at_least" ? 1 : 0;
            minimum.Text = section["min_pass"]?.ToString() ?? "1";
            onFail.SelectedIndex = (string?)section["on_fail"] == "restart" ? 1 : 0;
            rows.Clear();
            conditionPanel.Children.Clear();
            foreach (var row in section["conditions"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>())
                AddRow(row);
        }

        public void SetMode(bool allowRestart, string restartLabel)
        {
            allowRestartFlag = allowRestart;
            if (!showModeAndFail) return;
            ApplyFailureChoices(onFail, allowRestart, restartLabel);
        }

        public JObject Read(bool validate, bool includeModeAndFail)
        {
            var obj = new JObject
            {
                ["combine"] = combine.SelectedIndex == 0 ? "all" : "at_least",
                ["min_pass"] = Value(minimum, validate, 1, $"{sectionLabel}·最低达标项数", 1, 100),
                ["conditions"] = new JArray(rows.Select(row => row.Read(validate, sectionLabel))),
            };
            if (includeModeAndFail)
            {
                obj["mode"] = mode.SelectedIndex == 0 ? "filter" : "observe";
                obj["on_fail"] = allowRestartFlag && onFail.SelectedIndex == 1 ? "restart" : "pause";
            }
            return obj;
        }
    }

    private sealed class ConditionRow : StackPanel
    {
        private static readonly string[] Keys =
        {
            "stat_sum_min", "realtime_luck_min", "stat_min", "adaptation_points_min", "supporter_points_min",
            "bond_ready_min", "bond_card_min", "friend_outing_unlocked", "chain_stage_min", "chain_complete",
            "cut_event_completed", "cut_present",
        };
        private readonly ComboBox kind = Choice(
            "五维总和最低值", "胡度值最低门槛（-30～30）", "指定属性最低值", "适应性Pt最低值", "协助者积分最低值",
            "羁绊达线最低张数", "指定卡羁绊最低值", "友人外出已解锁", "指定卡事件段数", "指定卡事件已完走",
            "大小切事件已走完", "已确认才能出众");
        private readonly ComboBox stat = Choice("速度", "耐力", "力量", "毅力", "智力");
        private readonly TextBox minimum = Input("", "最低值 / 张数 / 段数（胡度可填负数）");
        private readonly TextBox card = Input("", "支援卡ID（指定卡条件必填）");
        private readonly TextBox bond = Input("80", "羁绊线");
        private readonly TextBox chain = Input("", "事件链ID（同卡有多个链时填写）");
        private readonly ComboBox cut = Choice("大切", "小切");
        public Button Remove { get; } = new() { Content = "移除条件" };

        public ConditionRow(JObject value)
        {
            Spacing = 5;
            Children.Add(kind);
            Children.Add(stat);
            Children.Add(minimum);
            Children.Add(card);
            Children.Add(bond);
            Children.Add(chain);
            Children.Add(cut);
            Children.Add(Remove);
            kind.SelectedIndex = Math.Max(0, Array.IndexOf(Keys, (string?)value["kind"]));
            stat.SelectedIndex = Math.Max(0, Array.IndexOf(new[] { "speed", "stamina", "power", "guts", "wisdom" }, (string?)value["stat"]));
            minimum.Text = value["minimum"]?.ToString() ?? "";
            card.Text = (string?)value["card_id"] ?? "";
            bond.Text = value["bond_threshold"]?.ToString() ?? "80";
            chain.Text = (string?)value["chain_id"] ?? "";
            cut.SelectedIndex = (string?)value["event_kind"] == "small" ? 1 : 0;
            kind.SelectionChanged += (_, _) => Render();
            Render();
        }

        private void Render()
        {
            if (kind.SelectedIndex < 0 || kind.SelectedIndex >= Keys.Length) return;
            var key = Keys[kind.SelectedIndex];
            stat.IsVisible = key == "stat_min";
            minimum.IsVisible = !new[] { "friend_outing_unlocked", "chain_complete", "cut_event_completed", "cut_present" }.Contains(key);
            card.IsVisible = key is "bond_card_min" or "chain_stage_min" or "chain_complete";
            bond.IsVisible = key == "bond_ready_min";
            chain.IsVisible = key is "chain_stage_min" or "chain_complete";
            cut.IsVisible = key == "cut_event_completed";
        }

        public JObject Read(bool validate, string sectionLabel = "节点")
        {
            var key = Keys[Math.Max(0, kind.SelectedIndex)];
            var value = new JObject { ["kind"] = key };
            if (minimum.IsVisible)
            {
                var minText = (minimum.Text ?? "").Trim();
                if (validate && string.IsNullOrWhiteSpace(minText))
                {
                    minimum.BorderBrush = Brushes.IndianRed;
                    throw new FormatException($"续养配置：{sectionLabel}·条件最低值不能为空。");
                }
                if (key == "realtime_luck_min")
                {
                    if (validate)
                    {
                        if (!double.TryParse(minText, NumberStyles.Float, CultureInfo.InvariantCulture, out var d)
                            || !double.IsFinite(d) || d < -30.0 || d > 30.0)
                        {
                            minimum.BorderBrush = Brushes.IndianRed;
                            throw new FormatException($"续养配置：{sectionLabel}·胡度值最低门槛请填写 -30 ～ 30 的数值。");
                        }
                        minimum.ClearValue(TemplatedControl.BorderBrushProperty);
                        value["minimum"] = Math.Abs(d - Math.Round(d)) < 1e-9 ? new JValue((long)Math.Round(d)) : new JValue(d);
                    }
                    else
                    {
                        if (double.TryParse(minText, NumberStyles.Float, CultureInfo.InvariantCulture, out var d)
                            && double.IsFinite(d))
                            value["minimum"] = Math.Abs(d - Math.Round(d)) < 1e-9 ? new JValue((long)Math.Round(d)) : new JValue(d);
                        else
                            value["minimum"] = new JValue(minText);
                    }
                }
                else if (key == "bond_ready_min")
                {
                    value["minimum"] = Value(minimum, validate, -1, $"{sectionLabel}·达线支援张数", 1, 6);
                }
                else if (key == "bond_card_min")
                {
                    value["minimum"] = Value(minimum, validate, -1, $"{sectionLabel}·关键卡羁绊线", 0, 100);
                }
                else if (key == "chain_stage_min")
                {
                    value["minimum"] = Value(minimum, validate, -1, $"{sectionLabel}·事件段数", 1, 20);
                }
                else
                {
                    value["minimum"] = Value(minimum, validate, -1, $"{sectionLabel}·条件最低值");
                }
            }
            if (stat.IsVisible)
                value["stat"] = new[] { "speed", "stamina", "power", "guts", "wisdom" }[Math.Max(0, stat.SelectedIndex)];
            if (card.IsVisible)
                value["card_id"] = card.Text?.Trim() ?? "";
            if (bond.IsVisible)
                value["bond_threshold"] = Value(bond, validate, 80, $"{sectionLabel}·羁绊线", 0, 100);
            if (chain.IsVisible && !string.IsNullOrWhiteSpace(chain.Text))
                value["chain_id"] = chain.Text.Trim();
            if (cut.IsVisible)
                value["event_kind"] = cut.SelectedIndex == 0 ? "big" : "small";
            return value;
        }
    }
}
