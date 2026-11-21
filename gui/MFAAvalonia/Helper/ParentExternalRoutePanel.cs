using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Search instructions, cached 吗哩吗哩 candidate pool, and local review for one explicitly selected pedigree slot.</summary>
internal sealed class ParentExternalRoutePanel : Expander
{
    private const string OfficialUrl = "https://game.bilibili.com/tool/pd/";
    private readonly Func<JObject, Task<JObject>> request;
    private readonly Func<JObject> context;
    private readonly Action<JObject, JObject> apply;
    private readonly Func<int, bool> canFill;
    private readonly Action? poolChanged;
    private readonly TextBlock status = Note("");
    private readonly TextBox search = new()
    {
        Name = "ParentExternalSearchText", IsReadOnly = true, AcceptsReturn = true,
        TextWrapping = TextWrapping.Wrap, IsVisible = false,
    };
    private readonly TextBox candidates = new()
    {
        Name = "ParentExternalCandidateText", Watermark = "粘贴种马卡片文字或官方搜索结果 JSON",
        AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxLength = 262144,
        MinHeight = 82, MaxHeight = 220,
    };
    private readonly Button prepare = new() { Name = "ParentExternalQuery", Content = "生成搜索条件" };
    private readonly Button copy = new() { Name = "ParentExternalCopy", Content = "复制搜索条件", IsEnabled = false };
    private readonly Button compare = new() { Name = "ParentExternalCompare", Content = "比较候选" };
    private readonly Button savePool = new() { Name = "ParentExternalSavePool", Content = "导入并存入候选池" };
    private readonly Button comparePool = new() { Name = "ParentExternalComparePool", Content = "用候选池补此位" };
    private readonly Button refreshPool = new() { Name = "ParentExternalPoolRefresh", Content = "刷新候选池", FontSize = 12 };
    private readonly Button clearPool = new() { Name = "ParentExternalPoolClear", Content = "清空候选池", FontSize = 12 };
    private readonly TextBlock poolSummary = Note("本地吗哩候选池：0 匹（粘贴卡片或 JSON 后可存入复用，自动配对也能直接调用）");
    private readonly StackPanel poolList = new() { Name = "ParentExternalPoolList", Spacing = 6 };
    private readonly Expander poolExpander;
    private readonly StackPanel results = new() { Name = "ParentExternalResults", Spacing = 8 };
    private int slot = -1;
    private int generation;
    private bool busy;
    private bool poolBusy;
    private JObject? searchContext;

    public ParentExternalRoutePanel(Func<JObject, Task<JObject>> request, Func<JObject> context,
        Action<JObject, JObject> apply, Func<int, bool> canFill, Action? poolChanged = null)
    {
        this.request = request;
        this.context = context;
        this.apply = apply;
        this.canFill = canFill;
        this.poolChanged = poolChanged;
        Name = "ParentExternalRoute";
        IsVisible = false;
        var openMaliTool = new Button { Name = "ParentExternalOpenMaliTool", Content = "🌐 内嵌吗哩一键查马" };
        var open = new Button { Name = "ParentExternalOpen", Content = "打开吗哩吗哩 ↗" };
        var controls = new WrapPanel();
        foreach (var button in new[] { prepare, copy, openMaliTool, open, compare, savePool, comparePool })
        {
            button.Margin = new Thickness(0, 0, 8, 5);
            controls.Children.Add(button);
        }
        var poolActions = new WrapPanel();
        foreach (var button in new[] { refreshPool, clearPool })
        {
            button.Margin = new Thickness(0, 0, 8, 4);
            poolActions.Children.Add(button);
        }
        poolExpander = new Expander
        {
            Name = "ParentExternalPoolExpander",
            Header = "本地吗哩候选池（0 匹）",
            IsExpanded = false,
            Content = new StackPanel
            {
                Spacing = 6,
                Children = { poolActions, poolSummary, poolList },
            },
        };
        Content = new StackPanel
        {
            Spacing = 7,
            Children =
            {
                Note("保留树中其他素材。复制搜索条件，登录官方页面筛选，再把种马卡片或搜索结果带回；也可存入本地候选池免重复粘贴。"),
                controls, search, candidates, poolExpander, status, results,
            },
        };
        prepare.Click += async (_, _) => await Run(false, usePool: false);
        compare.Click += async (_, _) => await Run(true, usePool: false);
        comparePool.Click += async (_, _) => await Run(true, usePool: true);
        savePool.Click += async (_, _) => await SaveToPool();
        refreshPool.Click += async (_, _) => await RefreshPool(silent: false);
        clearPool.Click += async (_, _) => await ClearPool();
        copy.Click += async (_, _) => await CopySearch();
        openMaliTool.Click += (_, _) => MaliSeedSearchBridge.OpenOrActivate(
            TopLevel.GetTopLevel(this) as Window,
            request,
            context,
            () => { _ = RefreshPool(silent: true); poolChanged?.Invoke(); });
        open.Click += (_, _) => OpenOfficial();
        candidates.TextChanged += (_, _) => { generation++; results.Children.Clear(); };
    }

    public void ShowSlot(int index, string label)
    {
        if (slot != index)
        {
            slot = index;
            ContextChanged();
        }
        Header = "吗哩吗哩补位 · " + label;
        IsVisible = IsExpanded = true;
        this.BringIntoView();
        _ = RefreshPool(silent: true);
    }

    public void ContextChanged()
    {
        generation++;
        results.Children.Clear();
        searchContext = null;
        search.Text = "";
        search.IsVisible = copy.IsEnabled = false;
        if (IsVisible) status.Text = "按当前草稿生成条件或比较候选。";
    }

    public async Task RefreshPoolSilent() => await RefreshPool(silent: true);

    private JObject Snapshot()
    {
        var value = context();
        value["slot_index"] = slot;
        return value;
    }

    private JObject PoolCommand(string mode)
    {
        var ctx = context();
        var cmd = new JObject
        {
            ["action"] = "parent_planning_external_compare",
            ["mode"] = mode,
        };
        if (ctx["library_id"] is JToken lib && lib.Type != JTokenType.Null) cmd["library_id"] = lib.DeepClone();
        if (ctx["account_id"] is JToken acc && acc.Type != JTokenType.Null) cmd["account_id"] = acc.DeepClone();
        return cmd;
    }

    private bool IsCurrent(JObject snapshot, int version) =>
        version == generation && JToken.DeepEquals(snapshot, Snapshot());

    private void SetButtonsEnabled(bool enabled)
    {
        prepare.IsEnabled = compare.IsEnabled = comparePool.IsEnabled = savePool.IsEnabled = enabled;
    }

    private void SetPoolButtonsEnabled(bool enabled)
    {
        refreshPool.IsEnabled = clearPool.IsEnabled = savePool.IsEnabled = enabled;
    }

    private async Task RefreshPool(bool silent)
    {
        if (poolBusy) return;
        poolBusy = true;
        SetPoolButtonsEnabled(false);
        try
        {
            var reply = await request(PoolCommand("pool_list"));
            RenderPool(reply);
            if (!silent)
                status.Text = $"已刷新本地吗哩候选池（共 {Number(reply["pool_count"]) ?? 0} 匹）。";
        }
        catch (InvalidOperationException error)
        {
            if (!silent) status.Text = error.Message;
        }
        catch
        {
            if (!silent) status.Text = "本地吗哩候选池暂不可读。";
        }
        finally
        {
            poolBusy = false;
            SetPoolButtonsEnabled(!busy);
        }
    }

    private async Task SaveToPool()
    {
        if (busy || poolBusy) return;
        if (string.IsNullOrWhiteSpace(candidates.Text))
        {
            status.Text = "请先粘贴要存入候选池的种马卡片文字或官方搜索结果 JSON。";
            return;
        }
        poolBusy = true;
        SetPoolButtonsEnabled(false);
        status.Text = "正在解析并存入本地吗哩候选池…";
        try
        {
            var cmd = PoolCommand("pool_save");
            cmd["candidates"] = candidates.Text;
            var reply = await request(cmd);
            RenderPool(reply);
            var added = Number(reply["added_count"]) ?? 0;
            var total = Number(reply["pool_count"]) ?? 0;
            status.Text = $"已存入本地吗哩候选池（本次新增 {added} 匹，池内共 {total} 匹）。";
            poolChanged?.Invoke();
        }
        catch (InvalidOperationException error) { status.Text = error.Message; }
        catch { status.Text = "存入本地吗哩候选池失败，请检查粘贴内容格式。"; }
        finally
        {
            poolBusy = false;
            SetPoolButtonsEnabled(!busy);
        }
    }

    private async Task RemovePoolItem(string entryId)
    {
        if (busy || poolBusy || string.IsNullOrWhiteSpace(entryId)) return;
        poolBusy = true;
        SetPoolButtonsEnabled(false);
        try
        {
            var cmd = PoolCommand("pool_remove");
            cmd["entry_id"] = entryId;
            var reply = await request(cmd);
            RenderPool(reply);
            status.Text = $"已从本地候选池移除（剩余 {Number(reply["pool_count"]) ?? 0} 匹）。";
            poolChanged?.Invoke();
        }
        catch (InvalidOperationException error) { status.Text = error.Message; }
        catch { status.Text = "移除候选失败，请重试。"; }
        finally
        {
            poolBusy = false;
            SetPoolButtonsEnabled(!busy);
        }
    }

    private async Task ClearPool()
    {
        if (busy || poolBusy) return;
        poolBusy = true;
        SetPoolButtonsEnabled(false);
        try
        {
            var reply = await request(PoolCommand("pool_clear"));
            RenderPool(reply);
            status.Text = "已清空本地吗哩候选池。";
            poolChanged?.Invoke();
        }
        catch (InvalidOperationException error) { status.Text = error.Message; }
        catch { status.Text = "清空候选池失败，请重试。"; }
        finally
        {
            poolBusy = false;
            SetPoolButtonsEnabled(!busy);
        }
    }

    private void RenderPool(JObject reply)
    {
        var items = (reply["pool"] as JArray ?? new()).OfType<JObject>().ToArray();
        var count = Number(reply["pool_count"]) ?? items.Length;
        poolExpander.Header = $"本地吗哩候选池（{count} 匹）";
        poolSummary.Text = count == 0
            ? "本地吗哩候选池为空。粘贴官方种马卡片或搜索结果 JSON 后点击“导入并存入候选池”。"
            : $"已缓存 {count} 匹吗哩候选（可直接在任意空位点“用候选池补此位”，或在下方一键全局配对中勾选纳入）。";
        poolList.Children.Clear();
        foreach (var item in items)
        {
            var entryId = Text(item["entry_id"]);
            var label = Text(item["label"]);
            if (label.Length == 0) label = entryId;
            var selfSummary = string.Join(" · ", (item["self_factors"] as JArray ?? new()).OfType<JObject>()
                .Select(f => $"{Text(f["raw_name"])}{Number(f["stars"]) ?? 0}★")
                .Where(s => s.Length > 0));
            var totalSummary = string.Join(" · ", (item["reference_totals"] as JArray ?? new()).OfType<JObject>()
                .Select(t => $"{Text(t["factor_key"]).Replace("red:", "").Replace("blue:", "").Replace("white:", "")}家系{Number(t["family_stars"]) ?? 0}★")
                .Where(s => s.Length > 0));
            var info = new StackPanel { Spacing = 2 };
            info.Children.Add(new TextBlock { Text = label, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap, FontSize = 12 });
            if (selfSummary.Length > 0) info.Children.Add(Note("本体：" + selfSummary));
            if (totalSummary.Length > 0) info.Children.Add(Note("家系参考：" + totalSummary));

            var removeBtn = new Button { Content = "移除", FontSize = 11, Padding = new Thickness(8, 3), VerticalAlignment = VerticalAlignment.Center };
            removeBtn.Click += async (_, _) => await RemovePoolItem(entryId);
            var rowGrid = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 8 };
            rowGrid.Children.Add(info);
            Grid.SetColumn(removeBtn, 1);
            rowGrid.Children.Add(removeBtn);
            poolList.Children.Add(new Border
            {
                Padding = new Thickness(7), BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6), BorderBrush = Brushes.Gray, Child = rowGrid,
            });
        }
    }

    private async Task Run(bool comparing, bool usePool)
    {
        if (busy) return;
        if (slot < 0 || !canFill(slot))
        {
            status.Text = "这个位置随已选亲代固定。请先选择可以补位的节点。";
            return;
        }
        var snapshot = Snapshot();
        if (snapshot["target_character_id"]?.Type != JTokenType.Integer)
        {
            status.Text = "请先选择育成目标。";
            return;
        }
        if (comparing && !usePool && string.IsNullOrWhiteSpace(candidates.Text))
        {
            status.Text = "请先粘贴要比较的种马资料，或点击“用候选池补此位”。";
            return;
        }
        var version = generation;
        var command = (JObject)snapshot.DeepClone();
        command["action"] = comparing ? "parent_planning_external_compare" : "parent_planning_external_query";
        if (comparing)
        {
            if (usePool) command["use_pool"] = true;
            if (!string.IsNullOrWhiteSpace(candidates.Text)) command["candidates"] = candidates.Text;
        }
        busy = true;
        SetButtonsEnabled(false);
        status.Text = comparing ? (usePool ? "正在从本地候选池筛选并比较…" : "正在按当前草稿比较…") : "正在核算搜索条件…";
        try
        {
            var reply = await request(command);
            if (!IsCurrent(snapshot, version)) return;
            if (comparing)
            {
                if (Number(reply["pool_count"]) is int pc)
                    poolExpander.Header = $"本地吗哩候选池（{pc} 匹）";
                RenderCandidates(reply, snapshot, version);
            }
            else
            {
                search.Text = Text(reply["search_text"]);
                search.IsVisible = copy.IsEnabled = !string.IsNullOrWhiteSpace(search.Text);
                searchContext = copy.IsEnabled ? snapshot : null;
                var msg = copy.IsEnabled ? "条件已生成，可以复制到搜索器，也可在官方页面手动填写。" : "当前草稿没有返回搜索条件。";
                if ((reply["avoid_a_limits"] as JArray)?.Count > 0 && Text(reply["note"]) is string note && note.Length > 0)
                    msg += "\n" + note;
                status.Text = msg;
            }
        }
        catch (InvalidOperationException error) { if (IsCurrent(snapshot, version)) status.Text = error.Message; }
        catch { if (IsCurrent(snapshot, version)) status.Text = "本次补位没有完成，草稿已保留。"; }
        finally { busy = false; SetButtonsEnabled(true); }
    }

    private void RenderCandidates(JObject reply, JObject snapshot, int version)
    {
        results.Children.Clear();
        var rows = (reply["suggestions"] as JArray ?? new()).OfType<JObject>().ToArray();
        status.Text = rows.Length == 0 ? "没有可应用的候选，请检查资料或调整条件。" : $"已复核 {rows.Length} 个候选。";
        foreach (var row in rows)
        {
            var route = row["local_route"] as JObject;
            var review = row["local_review"] as JObject;
            var conditional = row["conditional_review"] as JObject;
            var body = new StackPanel { Spacing = 5 };
            body.Children.Add(new TextBlock { Text = Text(row["label"]), FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
            body.Children.Add(Note("当前资料 · " + ReviewLabel(review)));
            if (conditional != null)
                body.Children.Add(Note("若这匹可借 · " + ReviewLabel(conditional, Number(row["conditional_total_stars"]))));
            if (Number(row["reported_family_stars"]) is int reported)
                body.Children.Add(Note($"吗哩吗哩报告这一支合计 {reported} 星，祖辈逐匹星级以资料为准。"));
            var compatibility = row["compatibility"] as JObject;
            if (Number(compatibility?["total"]) is int total)
                body.Children.Add(Note($"基础相性 {total}"));
            else if (Number(compatibility?["known_subtotal"]) is int known)
                body.Children.Add(Note($"已知基础相性 {known}，家系资料待补"));
            foreach (var apt in (row["aptitude_checks"] as JArray ?? new()).OfType<JObject>())
            {
                var label = Text(apt["label"]);
                if (label.Length == 0) continue;
                var cond = Number(apt["conditional_stars"]);
                var need = Number(apt["required_stars"]);
                var rem = Number(apt["remaining_gap"]);
                body.Children.Add(Note($"适性 {label} · " + (cond is int cs ? $"条件红星 {cs}" : "红星待补")
                    + (need is int ns ? $" / 目标 {ns}" : "")
                    + (rem is > 0 ? $"（仍缺 {rem} 星）" : need != null && cond != null ? "（已满足）" : "")));
            }
            foreach (var anti in (row["anti_a_checks"] as JArray ?? new()).OfType<JObject>())
            {
                var label = Text(anti["label"]);
                if (label.Length == 0) continue;
                var polluted = (bool?)anti["polluted"] == true;
                var cond = Number(anti["conditional_stars"]);
                var thresh = Number(anti["a_threshold_stars"]);
                var maxSafe = Number(anti["max_safe_stars"]);
                if (polluted)
                    body.Children.Add(Note($"⚠ 防污染越界：{label} 条件 {(cond?.ToString() ?? "未知")}★" + (thresh is int t ? $"（≥{t}★ 升 A！）" : "（已升 A！）")));
                else
                    body.Children.Add(Note($"🔒 防污染：{label} 条件 {cond ?? 0}★" + (maxSafe is int ms && ms < 18 ? $" / 安全上限 {ms}★" : "（安全）")));
            }
            foreach (var factor in (row["factor_checks"] as JArray ?? new()).OfType<JObject>())
            {
                var name = Text(factor["name"]);
                if (name.Length == 0) continue;
                var stars = Number(factor["reference_stars"]);
                var need = Number(factor["required_stars"]);
                body.Children.Add(Note(name + " · " + (stars is int count ? $"参考 {count} 星" : "星级待补")
                    + (need is int required ? $" / 目标 {required} 星" : "")));
            }
            var issues = (review?["selection_issues"] as JArray ?? new())
                .Concat(conditional?["selection_issues"] as JArray ?? new()).Select(Text)
                .Where(x => x.Length > 0).Distinct();
            foreach (var issue in issues) body.Children.Add(Note(issue));
            var use = new Button { Name = "ParentExternalApply", Content = "应用到当前草稿", IsEnabled = route != null && review != null };
            use.Click += (_, _) =>
            {
                if (!IsCurrent(snapshot, version)) { status.Text = "草稿或账号已变化，请重新比较候选。"; return; }
                if (route == null || review == null) return;
                apply((JObject)route.DeepClone(), (JObject)review.DeepClone());
                status.Text = "候选已填入草稿，可继续编辑或保存方案。";
            };
            body.Children.Add(use);
            results.Children.Add(new Border { Padding = new Thickness(9), BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8), BorderBrush = Brushes.Gray, Child = body });
        }
    }

    private static string ReviewLabel(JObject? review, int? hypotheticalStars = null)
    {
        var apt = review?["aptitude"] as JObject;
        var antiStatus = Text(apt?["anti_a"]?["status"]);
        var antiSuffix = antiStatus is "polluted" or "planned_polluted" ? " · ⚠ 触发禁升 A 污染" : "";
        var items = apt?["items"] as JArray;
        if (items != null && items.Count == 0) return "未设开局改适性" + antiSuffix;
        var plan = apt?["plan"];
        var state = Text(apt?["status"]).Length > 0 ? Text(apt?["status"]) : Text(plan?["status"]);
        var stars = hypotheticalStars ?? Number(plan?["known_stars"]);
        var required = Number(plan?["required_stars"]);
        var suffix = state switch {
            "slot_capacity_exceeded" => " · 超出 6 槽物理上限",
            "unreachable_at_start" => " · 含开局不可达目标",
            _ => ""
        };
        if (stars == null) return "红星资料待补" + suffix + antiSuffix;
        return $"红星 {stars}" + (required is int need ? $" / 目标 {need}" : "") + suffix + antiSuffix;
    }

    private async Task CopySearch()
    {
        if (searchContext == null || !JToken.DeepEquals(searchContext, Snapshot()))
        {
            status.Text = "草稿或目标已变化，请重新生成搜索条件。";
            return;
        }
        var clipboard = TopLevel.GetTopLevel(this)?.Clipboard;
        if (clipboard == null || string.IsNullOrWhiteSpace(search.Text)) return;
        try { await clipboard.SetTextAsync(search.Text); }
        catch { status.Text = "剪贴板暂不可用，可手动选中搜索条件复制。"; }
    }

    private void OpenOfficial()
    {
        try { using var browser = Process.Start(new ProcessStartInfo(OfficialUrl) { UseShellExecute = true }); }
        catch { status.Text = "浏览器未打开，可手动访问 " + OfficialUrl; }
    }

    private static string Text(JToken? value) => value?.Type == JTokenType.String ? (string?)value ?? "" : "";
    private static int? Number(JToken? value) => value?.Type == JTokenType.Integer ? (int?)value : null;
    private static TextBlock Note(string text) => new() { Text = text, FontSize = 12, TextWrapping = TextWrapping.Wrap };
}
