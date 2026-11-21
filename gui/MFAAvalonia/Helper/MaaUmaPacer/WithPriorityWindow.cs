using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

public sealed class WithPriorityWindow : Window
{
    private static readonly string[] Characters = {
        "神鹰", "里见光钻", "佐岳鸣", "天狼星象征", "曼城茶座", "中山庆典", "黄金船"
    };
    private readonly List<string> priority = new();
    private readonly ListBox list = new() { Name = "WithPriorityList", MinHeight = 140, MaxHeight = 290 };
    private readonly TextBlock status = Note("保存后对后续 With 事件生效，不会立即操作游戏。");

    private static List<string> SavedPriority(JObject? preferences)
    {
        if (preferences?.ContainsKey("with_priority") == true)
        {
            if (preferences["with_priority"] is not JArray values ||
                values.Any(value => value.Type != JTokenType.String || string.IsNullOrWhiteSpace((string?)value)))
                throw new InvalidOperationException("已保存的 With 优先级格式无效，请重新选择角色后保存。");
            return values.Select(value => ((string)value!).Trim()).Distinct().ToList();
        }
        var legacy = (string?)preferences?["with_target"];
        return string.IsNullOrWhiteSpace(legacy) ? new() : new() { legacy.Trim() };
    }

    public static string Summary(JObject? preferences)
    {
        try
        {
            var names = SavedPriority(preferences);
            return names.Count == 0 ? "自动选择" : string.Join(" → ", names);
        }
        catch (Exception error) { return error.Message; }
    }

    public WithPriorityWindow(JObject? preferences, Func<JArray, Task> save)
    {
        Title = "With · 角色选择优先级";
        Width = 760; Height = 650; MinWidth = 560; MinHeight = 440;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        var root = new Grid { Margin = new Avalonia.Thickness(20),
            RowDefinitions = new("Auto,*,Auto"), RowSpacing = 12 };
        var header = new StackPanel { Spacing = 8 };
        header.Children.Add(new TextBlock { Text = "With 角色优先级", FontSize = 24 });
        header.Children.Add(Note("从上到下选择第一个在游戏里出现的角色。每局人选和位置可能不同，按角色名字选择，不固定第几项。"));
        root.Children.Add(header);
        var body = new StackPanel { Spacing = 10, Margin = new Avalonia.Thickness(0, 0, 12, 8) };
        body.Children.Add(list);
        var addChoice = new ComboBox { Name = "WithPriorityCharacter", ItemsSource = Characters,
            SelectedIndex = 0, HorizontalAlignment = HorizontalAlignment.Stretch };
        var customName = new TextBox { Name = "WithPriorityCustomCharacter",
            Watermark = "其他角色：填写游戏显示的完整名字（可选）" };
        body.Children.Add(addChoice); body.Children.Add(customName);
        var actions = new WrapPanel { Orientation = Orientation.Horizontal };
        void Action(string name, string label, Action clicked)
        {
            var button = new Button { Name = name, Content = label, Margin = new Avalonia.Thickness(0, 0, 8, 4) };
            button.Click += (_, _) => clicked(); actions.Children.Add(button);
        }
        Action("WithPriorityAdd", "添加角色", () => {
            var name = string.IsNullOrWhiteSpace(customName.Text) ? addChoice.SelectedItem as string : customName.Text.Trim();
            if (string.IsNullOrWhiteSpace(name)) { status.Text = "请先选择一个角色。"; return; }
            if (priority.Contains(name)) { status.Text = "这个角色已在优先级列表中。"; return; }
            priority.Add(name); customName.Text = ""; Refresh(priority.Count - 1);
        });
        Action("WithPriorityRemove", "移除", () => {
            var index = list.SelectedIndex; if (index < 0) return;
            priority.RemoveAt(index); Refresh(Math.Min(index, priority.Count - 1));
        });
        void Move(int step)
        {
            var index = list.SelectedIndex; var target = index + step;
            if (index < 0 || target < 0 || target >= priority.Count) return;
            (priority[index], priority[target]) = (priority[target], priority[index]); Refresh(target);
        }
        Action("WithPriorityUp", "上移", () => Move(-1));
        Action("WithPriorityDown", "下移", () => Move(1));
        Action("WithPriorityAutomatic", "恢复自动选择", () => {
            priority.Clear(); Refresh(-1); status.Text = "已清空优先级，保存后恢复自动选择。";
        });
        body.Children.Add(actions);
        body.Children.Add(Note("列表为空，或这一局没有出现列表中的角色时，沿用现有自动选择。事件搜索里单独指定的选项仍然优先。"));
        body.Children.Add(Note("此处只指定角色，不承诺技能奖励或继承结果。其他事件设置中尚未保存的修改，仍需回到事件页保存。"));
        var scroll = new ScrollViewer { Content = body, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        Grid.SetRow(scroll, 1); root.Children.Add(scroll);
        var footer = new StackPanel { Spacing = 8 }; status.Name = "WithPriorityStatus"; footer.Children.Add(status);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10,
            HorizontalAlignment = HorizontalAlignment.Right };
        var saveButton = new Button { Name = "WithPrioritySave", Content = "保存 With 优先级" };
        var cancel = new Button { Name = "WithPriorityCancel", Content = "取消" };
        buttons.Children.Add(saveButton); buttons.Children.Add(cancel); footer.Children.Add(buttons);
        Grid.SetRow(footer, 2); root.Children.Add(footer); Content = root;
        try { priority.AddRange(SavedPriority(preferences)); }
        catch (Exception error) { status.Text = error.Message; }
        Refresh(priority.Count > 0 ? 0 : -1, changed: false);
        cancel.Click += (_, _) => Close(false);
        saveButton.Click += async (_, _) => {
            root.IsEnabled = false;
            try { await save(ReadPriority()); Close(true); }
            catch (Exception error) { status.Text = error.Message; }
            finally { root.IsEnabled = true; }
        };
    }

    private static TextBlock Note(string text) => new() { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = 13 };
    private void Refresh(int selected, bool changed = true)
    {
        list.ItemsSource = priority.Select((name, index) => $"{index + 1}. {name}").ToArray();
        list.SelectedIndex = selected;
        if (changed) status.Text = "优先级已修改，点击保存后生效。";
    }
    public JArray ReadPriority() => new(priority);
}
