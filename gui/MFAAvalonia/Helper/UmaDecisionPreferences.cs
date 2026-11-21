using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Node-graph preferences UI, retaining the public preference integration API.</summary>
public sealed class UmaDecisionPreferences : StackPanel
{
    private readonly UmaWeightGraphEditor graph = new();
    private readonly UmaWeightTimelineEditor timeline;
    private readonly ContentControl strategyView = new() { Name = "PacerStrategyView", HorizontalContentAlignment = HorizontalAlignment.Stretch };
    private readonly ContentControl baseWeightsView = new() { Name = "PacerBaseWeightsView", IsVisible = false, HorizontalContentAlignment = HorizontalAlignment.Stretch };
    private readonly StackPanel strategyViewTools = new();
    private readonly StackPanel baseWeights = new() { Spacing = 8 };
    private readonly Expander baseWeightsSection;
    private readonly ComboBox strategyViewMode = new() { Name = "PacerStrategyViewMode", ItemsSource = new[] { "阶段表", "高级策略编辑" }, SelectedIndex = 0 };
    private readonly Button advancedEntry = new() { Name = "PacerAdvancedStrategy", Content = "高级策略编辑", Height = 32, Padding = new Thickness(10,5) };
    private readonly Button backToSheet = new() { Name = "PacerBackToStrategySheet", Content = "返回阶段表", IsVisible = false, Height = 32, Padding = new Thickness(10,5) };
    private readonly StackPanel advancedTools = new() { Name = "PacerAdvancedStrategyTools", Spacing = 10, IsVisible = false };
    private readonly HashSet<string> editedBase = new();
    private JObject? preservedWorkflow;
    private readonly UmaWeightScorePreview scorePreview;
    private readonly ComboBox numeric = Multiplier("PacerNumericPreference");
    private readonly ComboBox bond = Multiplier("PacerBondPreference");
    private readonly ComboBox charge = Multiplier("PacerChargePreference");
    private readonly NumericUpDown gold = Bonus("PacerGoldShipBonus", 0);
    private readonly NumericUpDown mei = Bonus("PacerMeiBonus", 5);
    private readonly Button detailed = new() { Name = "PacerDetailedWeights", Content = "高级：内置权重细调", IsVisible = false };
    private readonly Button written = new() { Name = "PacerFormulaWeights", Content = "高级：自写公式", IsVisible = false };
    private readonly Button quickDetailed = new() { Name = "PacerQuickDetailedWeights", Content = "高级：内置权重细调", IsVisible = false };
    private readonly Button quickFormula = new() { Name = "PacerQuickFormulaWeights", Content = "高级：自写公式", IsVisible = false };
    private readonly Button import = new() { Name = "PacerWorkflowImport", Content = "读取策略方案" };
    private readonly Button export = new() { Name = "PacerWorkflowExport", Content = "导出策略方案" };
    private readonly Button check = new() { Name = "PacerWorkflowValidate", Content = "校验策略" };
    private readonly Button generated = new() { Name = "PacerGeneratedWeightCode", Content = "查看完整策略文件" };
    private readonly TextBox generatedCode = new() { Name = "PacerGeneratedWeightJson", IsReadOnly = true, AcceptsReturn = true, MinHeight = 260, MaxHeight = 400, FontFamily = new FontFamily("Cascadia Mono, Consolas"), TextWrapping = TextWrapping.NoWrap };
    private readonly Expander generatedSection;
    private readonly TextBlock status = Note("策略随当前运行配置保存，新一轮生效。暂停继续沿用本轮设置。");
    private JObject basis = new();
    private string? strategyRevision;
    private decimal defaultMeiBonus=5;
    private bool preserveMissingBaseFields;
    private JObject? tuningSchema;
    private Func<JObject, Task<JObject>>? validator;
    private bool hasWorkflow;
    private bool loading;
    private bool busy;
    private bool baseWeightsOnly;
    public event Action? Changed;

    public UmaDecisionPreferences()
    {
        scorePreview = new UmaWeightScorePreview(ReadProfile);
        Spacing = 12;
        timeline = new UmaWeightTimelineEditor(() => graph.ReadGraph(), candidate => {
            graph.Load(UmaWeightGraphModel.Compile(candidate)); preservedWorkflow = null; hasWorkflow = true;
            scorePreview.InvalidatePreview(); Changed?.Invoke();
        }, id => { if(ChangeStrategyView(1))graph.FocusTimelineNode(timeline.SelectedStageId,id); });
        var viewTools = new WrapPanel { Orientation = Orientation.Horizontal };
        advancedEntry.Margin = new Thickness(0, 0, 8, 0);
        viewTools.Children.Add(advancedEntry); viewTools.Children.Add(backToSheet);
        advancedEntry.Click += (_, _) => ChangeStrategyView(1);
        backToSheet.Click += (_, _) => ChangeStrategyView(0);
        strategyViewTools.Children.Add(viewTools);strategyViewTools.Children.Add(strategyView);
        Children.Add(baseWeightsView);Children.Add(strategyViewTools);
        strategyViewMode.SelectionChanged += (_, _) => ShowStrategyView();
        ShowStrategyView();
        var files = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var button in new[] { import, export, check, generated, detailed, written }) { button.Margin = new Thickness(0, 0, 8, 8); files.Children.Add(button); }
        advancedTools.Children.Add(new Expander {Name="PacerStrategyFileTools",Header="导入、导出与评分工具",Content=files,IsExpanded=false,HorizontalContentAlignment=HorizontalAlignment.Stretch});
        var copyCode = new Button { Name = "PacerCopyGeneratedWeightCode", Content = "复制策略文件内容" };
        var codePanel = new StackPanel { Spacing = 8 }; codePanel.Children.Add(generatedCode); codePanel.Children.Add(copyCode);
        codePanel.Children.Add(Note("策略方案包含阶段安排、权重、画布位置和连线；导入后仍可继续编辑。"));
        generatedSection = new Expander { Name = "PacerGeneratedWeightSection", Header = "完整策略文件", Content = codePanel, IsExpanded = false, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        advancedTools.Children.Add(generatedSection);
        generated.Click += (_, _) => { try { generatedCode.Text = ExportJson(); generatedSection.IsExpanded = true; status.Text = "已生成完整策略方案，可复制或导出。"; } catch(Exception error) { status.Text = error.Message; } };
        copyCode.Click += async (_, _) => { try { var text = ExportJson(); generatedCode.Text = text; var top = TopLevel.GetTopLevel(this); if(top?.Clipboard != null) { await top.Clipboard.SetTextAsync(text); status.Text = "已复制完整权重代码。"; } } catch(Exception error) { status.Text = error.Message; } };

        var legacy = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var pair in new (string Label, Control Input)[] { ("数值收益", numeric), ("普通支援羁绊", bond), ("充电进度", charge), ("黄金船在场额外分", gold), ("黄帽在场额外分", mei) })
        {
            var cell = new StackPanel { Width = 155, Spacing = 5, Margin = new Thickness(0, 0, 12, 8) }; cell.Children.Add(Note(pair.Label)); cell.Children.Add(pair.Input); legacy.Children.Add(cell);
        }
        var reset = new Button { Name = "PacerResetPreferences", Content = "恢复基础倍率默认值（5项）" };
        reset.Click += (_, _) =>
        {
            foreach (var key in new[] { "numeric", "bond", "charge", "gold_ship_bonus", "mei_bonus" })
            {
                basis.Remove(key);
                editedBase.Remove(key);
            }
            SetBase(null);
            scorePreview.InvalidatePreview();
            UpdateStatus();
            status.Text = "已重置 5 项基础倍率为策略默认；请点击顶栏“保存配置”。";
            Changed?.Invoke();
        };
        var resetAll = new Button { Name = "PacerResetAllWeights", Content = "✦ 一键恢复全部官方纯净（含细调/公式/分段）", FontWeight = FontWeight.SemiBold };
        resetAll.Click += (_, _) =>
        {
            PurifyAll();
            status.Text = "已清除基础倍率、高级细调、自写公式并关闭分年阶段表；请点击顶栏“保存配置”。";
            Changed?.Invoke();
        };
        var baseActions = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var btn in new[] { reset, resetAll, quickDetailed, quickFormula })
        {
            btn.Margin = new Thickness(0, 0, 8, 6);
            baseActions.Children.Add(btn);
        }
        baseWeights.Children.Add(legacy); baseWeights.Children.Add(baseActions);
        baseWeights.Children.Add(Note("基础设置对全部阶段生效。分段策略启用时，独立评分和公式暂存；关闭分段策略后恢复。"));
        baseWeightsSection = new Expander { Name = "PacerLegacyPreferences", Header = "全年基础设置", Content = baseWeights, IsExpanded = false, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        advancedTools.Children.Add(baseWeightsSection);
        status.Name = "PacerAdvancedWeightsStatus"; Children.Add(status);
        var previewSection = new Expander { Name = "PacerScorePreview", Header = "示例回合 · 对比调整前后的得分", Content = scorePreview, IsExpanded = false, HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        advancedTools.Children.Add(previewSection); Children.Add(advancedTools);
        graph.ScorePreviewRequested += () => { previewSection.IsExpanded = true; scorePreview.BringIntoView(); };
        graph.DraftChanged += () => scorePreview.InvalidatePreview();
        graph.Changed += () => { if (loading) return; hasWorkflow = true; scorePreview.InvalidatePreview(); Changed?.Invoke(); };
        foreach (var pair in new (Control Control, string Key)[] { (numeric,"numeric"), (bond,"bond"), (charge,"charge"), (gold,"gold_ship_bonus"), (mei,"mei_bonus") })
            pair.Control.PropertyChanged += (_, e) => { if (!loading && (e.Property.Name is "SelectedIndex" or "Value")) { editedBase.Add(pair.Key); scorePreview.InvalidatePreview(); Changed?.Invoke(); } };
        detailed.Click += async (_, _) => await OpenEditor("detailed"); written.Click += async (_, _) => await OpenEditor("formula");
        quickDetailed.Click += async (_, _) => await OpenEditor("detailed"); quickFormula.Click += async (_, _) => await OpenEditor("formula");
        import.Click += async (_, _) => await ImportFile(); export.Click += async (_, _) => await ExportFile(); check.Click += async (_, _) => await Validate();
        Load(null); Configure(null, _ => throw new InvalidOperationException("评分校验服务尚未连接。"));
    }

    public JObject Read()
    {
        var value = (JObject)basis.DeepClone();
        void BaseValue(string key, JToken current) { if (editedBase.Contains(key) || !preserveMissingBaseFields && value.Property(key) == null) value[key] = current; }
        BaseValue("numeric", new JValue(Factor(numeric))); BaseValue("bond", new JValue(Factor(bond))); BaseValue("charge", new JValue(Factor(charge)));
        BaseValue("gold_ship_bonus", new JValue(gold.Value ?? 0)); BaseValue("mei_bonus", new JValue(mei.Value ?? defaultMeiBonus));
        var workflow = preservedWorkflow == null ? graph.Read() : (JObject)preservedWorkflow.DeepClone();
        if (preservedWorkflow != null || hasWorkflow && ((bool?)workflow["enabled"] == true || ((workflow["stages"] as JArray)?.Count ?? 0) > 0 || (((workflow["editor_graph"] as JObject)?["nodes"] as JArray)?.Count ?? 0) > 0)) value["workflow"] = workflow; else value.Remove("workflow");
        return value;
    }
    public JObject ReadProfile() => Profile(tuningSchema ?? throw new InvalidOperationException("评分语义信息尚未加载。"), Read());
    public JObject ProfileFor(JObject preferences) => Profile(tuningSchema ?? throw new InvalidOperationException("评分语义信息尚未加载。"), preferences);
    public static JObject Profile(JObject schema, JObject preferences)
    {
        var result = UmaScoreEditor.Profile(schema, preferences);
        if (preferences["workflow"] is JObject)
        {
            result["schema_version"] = schema["workflow_profile_schema_version"]?.DeepClone() ?? "uma.scoring_profile.v3";
            result["workflow_semantics"] = schema["workflow_semantics"]?.DeepClone() ?? throw new InvalidOperationException("当前版本未提供工作流语义信息。");
        }
        else if ((string?)result["schema_version"] == "uma.scoring_profile.v3") result["schema_version"] = "uma.scoring_profile.v2";
        return result;
    }
    public void Load(JObject? value)
    {
        loading = true;
        try
        {
            basis = value == null ? new JObject() : (JObject)value.DeepClone(); editedBase.Clear(); SetBase(value);
            hasWorkflow = value?["workflow"] is JObject; preservedWorkflow = null;
            try { graph.Load(value?["workflow"] as JObject); }
            catch(Exception error) { preservedWorkflow = value?["workflow"] is JObject opaque ? (JObject)opaque.DeepClone() : null; graph.Load(null); status.Text = "高级方案已原样保留，当前编辑器无法展开：" + error.Message; }
            ShowStrategyView();
            scorePreview.InvalidatePreview();
            if(preservedWorkflow == null)UpdateStatus();
        }
        finally { loading = false; }
    }
    internal static bool UsesBFamilyPreferences(string? revision) => StrategyPackCatalog.UsesBFamilyPreferences(revision);

    public void LoadForStrategy(JObject? value,string? revision,bool factoryDefaults=false,bool preserveMissingBaseFields=false)
    {
        this.preserveMissingBaseFields=preserveMissingBaseFields;
        strategyRevision=revision;defaultMeiBonus=UsesBFamilyPreferences(revision)?0:5;
        Load(value);SetStrategyContext(revision,factoryDefaults);
    }
    public void SetStrategyContext(string? revision,bool factoryDefaults=false)
    {
        strategyRevision=revision;defaultMeiBonus=UsesBFamilyPreferences(revision)?0:5;
        var prior=loading;loading=true;
        try {
            if(!editedBase.Contains("mei_bonus")&&(factoryDefaults||basis.Property("mei_bonus")==null)) {
                basis.Remove("mei_bonus");mei.Value=defaultMeiBonus;
            }
        } finally {loading=prior;}
        scorePreview.InvalidatePreview();UpdateStatus();
    }
    public void Configure(JObject? schema, Func<JObject, Task<JObject>> validate, Func<JObject, Task<JObject>>? preview = null)
    {
        tuningSchema = schema; validator = validate; graph.Configure(schema); timeline.Configure(schema); scorePreview.Configure(preview);
        detailed.IsVisible = written.IsVisible = quickDetailed.IsVisible = quickFormula.IsVisible = schema != null;
        import.IsEnabled = export.IsEnabled = check.IsEnabled = generated.IsEnabled = schema != null; UpdateStatus();
    }

    /// <summary>Loads an editable demonstration without starting a task or changing other options.</summary>
    public void LoadTimelineExample() { preservedWorkflow = null; hasWorkflow = true; graph.LoadExample(); timeline.Refresh(); }
    public void LoadGraphExample() => LoadTimelineExample();
    public JObject ReadGraph() => graph.ReadGraph();

    private bool ChangeStrategyView(int target)
    {
        if(target==0 && strategyViewMode.SelectedIndex==1) {
            try { graph.ReadGraph(); }
            catch(Exception error) { status.Text="请先修正当前策略输入："+error.Message;return false; }
        }
        strategyViewMode.SelectedIndex=target;
        UpdateStatus();
        return true;
    }

    private void ShowStrategyView()
    {
        var advanced = strategyViewMode.SelectedIndex == 1;
        advancedEntry.IsVisible = !advanced; backToSheet.IsVisible = advanced; advancedTools.IsVisible = advanced && !baseWeightsOnly;
        strategyView.Content = null;
        if(preservedWorkflow != null) {
            strategyView.Content = Note("此高级图的全部节点、参数和连线已保留。当前版本不能无损编辑，可导出原方案或使用兼容版本打开。"); return;
        }
        if(strategyViewMode.SelectedIndex == 1) strategyView.Content = graph;
        else { strategyView.Content = timeline; timeline.Refresh(); }
    }

    internal void ShowBaseWeightsOnly(bool visible)
    {
        if(baseWeightsOnly==visible)return;
        baseWeightsOnly=visible;
        if(visible){baseWeightsSection.Content=null;baseWeightsView.Content=baseWeights;}
        else{baseWeightsView.Content=null;baseWeightsSection.Content=baseWeights;}
        baseWeightsView.IsVisible=visible;
        strategyViewTools.IsVisible=!visible;
        advancedTools.IsVisible=!visible&&strategyViewMode.SelectedIndex==1;
    }

    private void SetBase(JObject? value)
    {
        var prior = loading; loading = true;
        numeric.SelectedIndex = Index(value, "numeric"); bond.SelectedIndex = Index(value, "bond"); charge.SelectedIndex = Index(value, "charge");
        gold.Value = Math.Clamp((decimal?)value?["gold_ship_bonus"] ?? 0, 0, 20); mei.Value = Math.Clamp((decimal?)value?["mei_bonus"] ?? defaultMeiBonus, 0, 20); loading = prior;
    }
    public void PurifyAll()
    {
        var prior = loading;
        loading = true;
        try
        {
            foreach (var key in new[] { "numeric", "bond", "charge", "gold_ship_bonus", "mei_bonus" })
            {
                basis.Remove(key);
                editedBase.Remove(key);
            }
            basis.Remove("tuning");
            if (basis["workflow"] is JObject wf)
            {
                wf["enabled"] = false;
                if (wf["editor_graph"] is JObject eg)
                    eg["enabled"] = false;
                preservedWorkflow = null;
                graph.Load(wf);
                hasWorkflow = true;
            }
            SetBase(null);
            scorePreview.InvalidatePreview();
            UpdateStatus();
        }
        finally { loading = prior; }
    }

    private void UpdateStatus()
    {
        var tuning = basis["tuning"] as JObject;
        var mode = (string?)tuning?["mode"];
        var tuningNote = "";
        if (mode == "formula")
        {
            var f = ((string?)tuning?["formula"] ?? "").Trim();
            tuningNote = !string.IsNullOrEmpty(f) && !string.Equals(f, "base_score", StringComparison.Ordinal)
                ? " 当前有自写公式。"
                : "（当前为默认公式 base_score，未改变分数）";
        }
        else if (mode == "detailed")
        {
            var nonDefaultCount = (tuning?["factors"] as JObject)?.Properties()
                .Count(p => p.Value is JValue fv && fv.Type is JTokenType.Float or JTokenType.Integer && Math.Abs(fv.Value<double>() - 1d) > 1e-5) ?? 0;
            tuningNote = nonDefaultCount > 0
                ? $" 当前有 {nonDefaultCount} 项高级细调。"
                : "（存在空细调壳，未改变分数）";
        }
        var wfNote = basis["workflow"]?["enabled"]?.Type == JTokenType.Boolean && (bool)basis["workflow"]!["enabled"]!
            ? " 当前启用了分年阶段表。"
            : "";
        var scope = UsesBFamilyPreferences(strategyRevision)
            ? " 首年采用统一估值 B；数值、羁绊、充电及在场额外分各作用一次。黄帽内置前半+5、后半+3，本表填写额外分。首年的高级细调、公式和分段评分暂存，供后续阶段和旧策略使用。"
            : "";
        status.Text = "保存到当前运行配置，新一轮生效。" + tuningNote + wfNote + scope;
    }
    private async Task OpenEditor(string entry)
    {
        if (tuningSchema == null || validator == null || TopLevel.GetTopLevel(this) is not Window parent) return;
        JObject preferencesForAdvanced;
        try { preferencesForAdvanced = Read(); } catch(Exception error) { status.Text = error.Message; return; }
        var workflowDraft = preferencesForAdvanced["workflow"]?.DeepClone(); preferencesForAdvanced.Remove("workflow");
        async Task<JObject> ValidateAdvanced(JObject profile)
        {
            var preferences = (JObject?)profile["preferences"] ?? throw new InvalidOperationException("配置缺少偏好内容。");
            if (preferences["workflow"] is JObject) throw new InvalidOperationException("请在高级策略编辑中读取完整策略方案；此入口只读取公式与独立评分。");
            return await validator(Profile(tuningSchema, preferences));
        }
        var editor = new UmaScoreEditor(tuningSchema, preferencesForAdvanced, ValidateAdvanced, entry) { Title = "高级评分 · 应用后切回独立评分，保留分段策略草稿" };
        var result = await editor.ShowDialog<JObject?>(parent);
        if (result != null) { if (workflowDraft is JObject workflow) { workflow["enabled"] = false; if(workflow["editor_graph"] is JObject keptGraph) keptGraph["enabled"] = false; result["workflow"] = workflow; } Load(result); status.Text = "已切换到独立评分，分段策略草稿保留；重新启用分段策略后使用原安排。"; Changed?.Invoke(); }
    }
    public async Task ImportJson(string text)
    {
        if (text.Length > 8388608) throw new InvalidOperationException("配置文件过大。");
        if (validator == null || tuningSchema == null) throw new InvalidOperationException("评分校验服务尚未连接。");
        var result = await validator(JObject.Parse(text));
        Load((JObject?)result["preferences"] ?? throw new InvalidOperationException("后台没有返回有效配置。"));
        status.Text = "已读取并校验策略方案。保存当前配置后用于新一轮。"; Changed?.Invoke();
    }
    public string ExportJson() => ReadProfile().ToString(Formatting.Indented);
    private async Task Validate()
    {
        if (busy || validator == null || tuningSchema == null) return; Busy(true);
        try { var result = await validator(ReadProfile()); if (result["preferences"] is not JObject preferences) throw new InvalidOperationException("后台没有返回有效配置。"); Load(preferences); status.Text = "当前安排有效。保存面板配置后用于新一轮。"; }
        catch (Exception error) { status.Text = error.Message; } finally { Busy(false); }
    }
    private async Task ImportFile()
    {
        if (busy || TopLevel.GetTopLevel(this) is not TopLevel top) return;
        try
        {
            var files = await top.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions { Title = "读取育成策略方案", AllowMultiple = false, FileTypeFilter = new[] { new FilePickerFileType("JSON 策略方案") { Patterns = new[] { "*.json" } } } });
            if (files.Count == 0) return; Busy(true);
            await using var stream = await files[0].OpenReadAsync(); using var reader = new StreamReader(stream, Encoding.UTF8, true);
            var buffer = new char[8388609]; var count = await reader.ReadBlockAsync(buffer.AsMemory()); if (count > 8388608) throw new InvalidOperationException("配置文件过大。"); await ImportJson(new string(buffer, 0, count));
        }
        catch (Exception error) { status.Text = error.Message; } finally { Busy(false); }
    }
    private async Task ExportFile()
    {
        if (busy || validator == null || tuningSchema == null || TopLevel.GetTopLevel(this) is not TopLevel top) return; Busy(true);
        try
        {
            var result = await validator(ReadProfile()); Load((JObject?)result["preferences"] ?? throw new InvalidOperationException("后台没有返回有效配置。"));
            var file = await top.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions { Title = "保存育成策略方案", SuggestedFileName = "MaaUma-育成策略.json", DefaultExtension = "json", FileTypeChoices = new[] { new FilePickerFileType("JSON 策略方案") { Patterns = new[] { "*.json" } } } });
            if (file == null) return; await using var stream = await file.OpenWriteAsync(); stream.SetLength(0); await using var writer = new StreamWriter(stream, new UTF8Encoding(false)); await writer.WriteAsync(ExportJson()); status.Text = "已导出画布与权重。";
        }
        catch (Exception error) { status.Text = error.Message; } finally { Busy(false); }
    }
    private void Busy(bool value)
    {
        busy = value; graph.IsEnabled = timeline.IsEnabled = advancedEntry.IsEnabled = backToSheet.IsEnabled = !value; import.IsEnabled = export.IsEnabled = check.IsEnabled = generated.IsEnabled = !value && tuningSchema != null; detailed.IsEnabled = written.IsEnabled = quickDetailed.IsEnabled = quickFormula.IsEnabled = !value;
    }
    private static ComboBox Multiplier(string name) => new() { Name = name, ItemsSource = new[] { "80% · 降低", "100% · 默认", "120% · 提高" }, SelectedIndex = 1 };
    private static NumericUpDown Bonus(string name, decimal value) => new() { Name = name, Minimum = 0, Maximum = 20, Increment = 1, Value = value, FormatString = "0.##" };
    private static double Factor(ComboBox value) => new[] { .8, 1.0, 1.2 }[Math.Clamp(value.SelectedIndex, 0, 2)];
    private static int Index(JObject? value, string key) => (double?)value?[key] switch { .8 => 0, 1.2 => 2, _ => 1 };
    private static TextBlock Note(string text) => new() { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = 12 };
}
