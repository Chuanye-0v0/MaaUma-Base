using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

public sealed record EventSkillChoice(string Id, string Name, string Description, string DbId)
{
    public string Label => Name + (string.IsNullOrEmpty(DbId) ? " · " + Id : " · " + DbId);
    public override string ToString() => Label;
}

/// <summary>Edits event preferences only; candidate IDs never request skill purchases.</summary>
public sealed class EventSkillPicker : StackPanel
{
    internal static readonly IReadOnlyDictionary<string, string> Aliases =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["斗志昂扬"] = "斗志高昂",
            ["王手"] = "将军",
            ["弧线的教授"] = "弧线大师",
            ["弯道教授"] = "弧线大师",
            ["教授"] = "弧线大师",
            ["地固"] = "打基础",
            ["出闸"] = "专注力",
            ["金出闸"] = "专心一意",
            ["先手必胜"] = "先发制人",
            ["好转一息"] = "喘口气",
            ["金弯回"] = "圆弧艺术家",
            ["弯回"] = "弯道恢复○",
            ["直回"] = "直线恢复",
            ["垂马"] = "回避失速优俊少女",
        };

    private sealed record SelectedRow(JToken Value, string Label)
    {
        public override string ToString() => Label;
    }

    private readonly IReadOnlyList<EventSkillChoice> catalog;
    private readonly Dictionary<string, EventSkillChoice> byId;
    private readonly HashSet<string> duplicateNames;
    private readonly JArray values = new();
    private readonly TextBox search;
    private readonly ListBox results;
    private readonly TextBlock searchStatus;
    private readonly ListBox selected;
    private readonly Button add;
    private readonly Button remove;
    private readonly Button up;
    private readonly Button down;

    public event Action? Changed;
    public JArray Values => (JArray)values.DeepClone();
    public string DisplayText => string.Join("\n", values.Select(DisplayValue));

    public EventSkillPicker(string name, IReadOnlyList<EventSkillChoice> catalog)
    {
        Name = name;
        Spacing = 5;
        this.catalog = catalog;
        byId = catalog.GroupBy(row => row.Id, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.Ordinal);
        duplicateNames = catalog.GroupBy(row => row.Name, StringComparer.Ordinal)
            .Where(group => group.Select(row => row.Id).Distinct(StringComparer.Ordinal).Skip(1).Any())
            .Select(group => group.Key).ToHashSet(StringComparer.Ordinal);
        search = new TextBox { Name = name + "Search", Watermark = "搜索技能名、简称/黑话或编号（如：王手、地固、教授、出闸）" };
        results = new ListBox { Name = name + "Results", MaxHeight = 155, IsVisible = false };
        results.ItemTemplate = new FuncDataTemplate<EventSkillChoice>((row, _) => new StackPanel {
            Spacing = 2,
            Children = {
                new TextBlock { Text = row?.Label, TextWrapping = TextWrapping.Wrap },
                new TextBlock { Text = row?.Description, FontSize = 11, MaxHeight = 34,
                    TextWrapping = TextWrapping.Wrap, Opacity = .7 }
            }
        });
        searchStatus = new TextBlock { Name = name + "SearchStatus", FontSize = 12,
            TextWrapping = TextWrapping.Wrap };
        selected = new ListBox { Name = name + "Selected", MinHeight = 64, MaxHeight = 150 };
        add = ActionButton(name + "Add", "添加", AddSelected);
        remove = ActionButton(name + "Remove", "移除", RemoveSelected);
        up = ActionButton(name + "MoveUp", "上移", () => MoveSelected(-1));
        down = ActionButton(name + "MoveDown", "下移", () => MoveSelected(1));
        var actions = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var button in new[] { add, remove, up, down }) actions.Children.Add(button);
        Children.Add(search);
        Children.Add(results);
        Children.Add(searchStatus);
        Children.Add(selected);
        Children.Add(actions);
        search.TextChanged += (_, _) => Filter();
        results.SelectionChanged += (_, _) => UpdateButtons();
        results.DoubleTapped += (_, _) => AddSelected();
        selected.SelectionChanged += (_, _) => UpdateButtons();
        Filter();
        RenderSelected(-1);
    }

    private static Button ActionButton(string name, string label, Action action)
    {
        var button = new Button { Name = name, Content = label, Padding = new Thickness(11, 5),
            Margin = new Thickness(0, 0, 5, 0) };
        button.Click += (_, _) => action();
        return button;
    }

    public void SetValues(JToken? value)
    {
        values.RemoveAll();
        if (value is JArray array)
            foreach (var item in array) values.Add(item.DeepClone());
        else if (value?.Type == JTokenType.String)
            foreach (var line in ((string?)value ?? "").Split('\n'))
            {
                var item = line.TrimEnd('\r');
                if (!string.IsNullOrWhiteSpace(item)) values.Add(item);
            }
        RenderSelected(-1);
    }

    private string DisplayValue(JToken value)
    {
        var raw = value.Type == JTokenType.String ? (string?)value ?? "" : value.ToString();
        // Keep legacy names and unknown values intact. The existing backend owns normalization.
        if (!byId.TryGetValue(raw, out var row)) return raw;
        return duplicateNames.Contains(row.Name) ? row.Name + " · " + row.Id : row.Name;
    }

    private void Filter()
    {
        var query = (search.Text ?? "").Trim();
        if (query.Length == 0)
        {
            results.ItemsSource = Array.Empty<EventSkillChoice>();
            results.IsVisible = false;
            searchStatus.Text = catalog.Count == 0
                ? "技能候选暂不可用，已保存的技能仍保留，可调整顺序或移除。"
                : "输入技能名或常见简称（如 王手、教授、地固、出闸），可↑↓调整 #1 → #N 优先级。";
        }
        else
        {
            var aliasTargets = Aliases
                .Where(kv => kv.Key.Contains(query, StringComparison.OrdinalIgnoreCase))
                .Select(kv => kv.Value)
                .ToHashSet(StringComparer.OrdinalIgnoreCase);
            var matches = catalog.Where(row =>
                (row.Name + " " + row.Description + " " + row.DbId + " " + row.Id)
                .Contains(query, StringComparison.OrdinalIgnoreCase)
                || aliasTargets.Contains(row.Name)).ToArray();
            results.ItemsSource = matches.Take(40).ToArray();
            results.IsVisible = matches.Length > 0;
            results.SelectedIndex = matches.Length > 0 ? 0 : -1;
            searchStatus.Text = matches.Length == 0 ? "没有匹配技能，请换一个关键词。"
                : $"找到 {matches.Length} 项，显示 {Math.Min(40, matches.Length)} 项。按 #1 → #N 顺序优先，同一事件兼得时选排在前面的技能。";
        }
        UpdateButtons();
    }

    private void RenderSelected(int index)
    {
        selected.ItemsSource = values.Select((value, idx) => new SelectedRow(value, $"{idx + 1}. {DisplayValue(value)}")).ToArray();
        selected.SelectedIndex = index >= 0 && index < values.Count ? index : -1;
        UpdateButtons();
    }

    private bool AlreadySelected(string id) => values.Any(value => value.Type == JTokenType.String
        && string.Equals((string?)value, id, StringComparison.Ordinal));

    private void UpdateButtons()
    {
        add.IsEnabled = results.SelectedItem is EventSkillChoice row && !AlreadySelected(row.Id);
        var index = selected.SelectedIndex;
        remove.IsEnabled = index >= 0;
        up.IsEnabled = index > 0;
        down.IsEnabled = index >= 0 && index < values.Count - 1;
    }

    private void AddSelected()
    {
        if (results.SelectedItem is not EventSkillChoice row || AlreadySelected(row.Id)) return;
        values.Add(row.Id);
        RenderSelected(values.Count - 1);
        Changed?.Invoke();
    }

    private void RemoveSelected()
    {
        var index = selected.SelectedIndex;
        if (index < 0 || index >= values.Count) return;
        values.RemoveAt(index);
        RenderSelected(Math.Min(index, values.Count - 1));
        Changed?.Invoke();
    }

    private void MoveSelected(int delta)
    {
        var index = selected.SelectedIndex;
        if (index < 0 || index + delta < 0 || index + delta >= values.Count) return;
        var value = values[index];
        values.RemoveAt(index);
        values.Insert(index + delta, value);
        RenderSelected(index + delta);
        Changed?.Invoke();
    }

    public static IReadOnlyList<EventSkillChoice> LoadCatalog(string root, out string? error)
    {
        error = null;
        try
        {
            var appRoot = Path.GetFullPath(root);
            if (string.Equals(Path.GetFileName(Path.TrimEndingDirectorySeparator(appRoot)), "pacer", StringComparison.OrdinalIgnoreCase))
                appRoot = Path.GetDirectoryName(Path.TrimEndingDirectorySeparator(appRoot)) ?? appRoot;
            var path = Path.Combine(appRoot, "backend", "data", "harvest", "catalog.json");
            if (!File.Exists(path))
            {
                error = "未找到技能候选库；已保存的技能仍会保留。";
                return Array.Empty<EventSkillChoice>();
            }
            var data = JObject.Parse(File.ReadAllText(path));
            if ((int?)data["version"] != 1 || data["skills"] is not JArray skills)
                throw new InvalidDataException();
            var rows = new List<EventSkillChoice>();
            foreach (var item in skills.OfType<JObject>())
            {
                var id = (string?)item["id"];
                var name = (string?)item["name"];
                if (string.IsNullOrWhiteSpace(id) || string.IsNullOrWhiteSpace(name)) throw new InvalidDataException();
                rows.Add(new EventSkillChoice(id, name, (string?)item["description"] ?? "", (string?)item["db_id"] ?? ""));
            }
            if (rows.Count != skills.Count || rows.Select(row => row.Id).Distinct(StringComparer.Ordinal).Count() != rows.Count)
                throw new InvalidDataException();
            return rows;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or InvalidDataException
            or Newtonsoft.Json.JsonException or ArgumentException or InvalidOperationException or FormatException
            or InvalidCastException or OverflowException)
        {
            error = "技能候选库暂时无法读取；已保存的技能仍会保留，可稍后重试。";
            return Array.Empty<EventSkillChoice>();
        }
    }
}
