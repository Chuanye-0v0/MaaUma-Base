using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Manual actual wins, with explicit coverage and revision. No planned races are recorded.</summary>
public sealed class ParentLocalRouteWinsWindow : Window
{
    private readonly TextBox query=new(){Name="LocalWinsSearch",Watermark="输入赛事原名、编号或条件"};
    private readonly TextBox unknown=new(){Name="LocalWinsRawName",Watermark="本地目录未收录时，保留你看到的赛事原名"};
    private readonly CheckBox unknownWon=new(){Name="LocalWinsRawConfirmed",Content="我确认已获胜，仅赛事目录未对应",IsChecked=false};
    private readonly ComboBox coverage=new(){Name="LocalWinsCoverage",ItemsSource=new[]{"未录（未知）","部分记录","完整记录"},SelectedIndex=0};
    private readonly StackPanel results=new(){Name="LocalWinsResults",Spacing=5},selected=new(){Name="LocalWinsSelected",Spacing=5};
    private readonly TextBlock state=Note("仅填写实际已经获胜的赛事；计划参加的赛事不能记录成胜场。");
    private readonly List<JObject> wins=new();private int searchVersion;
    private readonly Func<string,Task<JObject>> search;private readonly Func<JObject,int,Task<JObject>> save;private readonly int revision;
    public ParentLocalRouteWinsWindow(string title,JObject? records,int revision,Func<string,Task<JObject>> search,Func<JObject,int,Task<JObject>> save)
    {
        this.search=search;this.save=save;this.revision=revision;
        Title=title+" · 实际胜场";Width=820;Height=780;MinWidth=480;MinHeight=440;WindowStartupLocation=WindowStartupLocation.CenterOwner;
        this.Bind(TextElement.ForegroundProperty,new DynamicResourceExtension("SukiText"));this.Bind(BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        foreach(var row in (records?["wins"] as JArray??new()).OfType<JObject>())wins.Add((JObject)row.DeepClone());coverage.SelectedIndex=(string?)records?["coverage"] switch{"complete"=>2,"partial"=>1,_=>0};
        var root=new Grid{Margin=new Thickness(20),RowDefinitions=new("Auto,*,Auto"),RowSpacing=12};var header=new StackPanel{Spacing=6};header.Children.Add(new TextBlock{Text=title+" · 实际胜场",FontSize=21});header.Children.Add(Note("胜场属于这一个真实节点，不会写入同名角色的其他成品。只有明确选择“完整记录”且清单为空，才表示确认 0 场。"));header.Children.Add(coverage);root.Children.Add(header);
        var body=new StackPanel{Spacing=10};var searchRow=new DockPanel();var find=new Button{Name="LocalWinsFind",Content="搜索",Margin=new Thickness(8,0,0,0)};DockPanel.SetDock(find,Dock.Right);searchRow.Children.Add(find);searchRow.Children.Add(query);body.Children.Add(searchRow);body.Children.Add(Note("目录中国服译名未核对的赛事显示原文和编号，不自动套用其他服译名。"));body.Children.Add(results);body.Children.Add(new Separator());body.Children.Add(new TextBlock{Text="已记录的实际胜场",FontSize=16});body.Children.Add(selected);body.Children.Add(unknown);body.Children.Add(unknownWon);var add=new Button{Name="LocalWinsAddRaw",Content="添加未收录原名"};body.Children.Add(add);
        var scroll=new ScrollViewer{Content=body,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled};Grid.SetRow(scroll,1);root.Children.Add(scroll);
        var footer=new StackPanel{Spacing=7};state.Name="LocalWinsStatus";footer.Children.Add(state);var buttons=new WrapPanel();var commit=new Button{Name="LocalWinsSave",Content="保存实际胜场",Margin=new Thickness(0,0,8,0)};var cancel=new Button{Name="LocalWinsCancel",Content="取消"};buttons.Children.Add(commit);buttons.Children.Add(cancel);footer.Children.Add(buttons);Grid.SetRow(footer,2);root.Children.Add(footer);Content=root;
        find.Click+=async(_,_)=>await Search();query.KeyDown+=async(_,e)=>{if(e.Key==Avalonia.Input.Key.Enter){e.Handled=true;await Search();}};
        add.Click+=(_,_)=>{var name=(unknown.Text??"").Trim();if(name.Length==0){state.Text="请先填写实际赛事原名。";return;}wins.Add(new JObject{["race_id"]="manual:"+Guid.NewGuid().ToString("N"),["raw_name"]=name,["status"]=unknownWon.IsChecked==true?"confirmed":"unverified",["source"]="manual"});unknown.Text="";unknownWon.IsChecked=false;MarkPartial();RenderSelected();};
        cancel.Click+=(_,_)=>Close(false);commit.Click+=async(_,_)=>{commit.IsEnabled=false;try{await save(ReadRecords(),revision);Close(true);}catch(InvalidOperationException e){state.Text=e.Message+" 当前清单保留；如版本冲突请关闭后重新读取，未自动覆盖。";}catch{state.Text="实际胜场未保存，当前清单仍保留。";}finally{commit.IsEnabled=true;}};RenderSelected();
    }
    private static TextBlock Note(string text)=>new(){Text=text,FontSize=12,TextWrapping=TextWrapping.Wrap};
    private static string Str(JToken? value)=>value?.Type is JTokenType.String or JTokenType.Integer?value.ToString():"";
    private void MarkPartial(){if(coverage.SelectedIndex==0)coverage.SelectedIndex=1;}
    public JObject ReadRecords()=>new(){["coverage"]=coverage.SelectedIndex switch{2=>"complete",1=>"partial",_=>"unknown"},["wins"]=new JArray(wins.Select(x=>x.DeepClone())),["source"]="manual"};
    private void RenderSelected()
    {
        selected.Children.Clear();if(wins.Count==0)selected.Children.Add(Note(coverage.SelectedIndex==2?"当前为空；保存时将明确记录为完整的 0 场。":"尚未录入胜场；不代表实际 0 场。"));
        foreach(var row in wins.ToArray()){var line=new DockPanel();var remove=new Button{Content="移除",Margin=new Thickness(8,0,0,0)};DockPanel.SetDock(remove,Dock.Right);remove.Click+=(_,_)=>{wins.Remove(row);RenderSelected();};line.Children.Add(remove);line.Children.Add(Note((Str(row["raw_name"]) is string name&&name.Length>0?name:"赛事 #"+Str(row["race_id"]))+" · "+Str(row["race_id"])+((string?)row["status"]=="confirmed"?" · 已确认胜场":" · 原名未核对")));selected.Children.Add(line);}
        coverage.SelectionChanged-=CoverageChanged;coverage.SelectionChanged+=CoverageChanged;
    }
    private void CoverageChanged(object? sender,SelectionChangedEventArgs e)=>RenderSelected();
    public async Task Search()
    {
        var at=++searchVersion;try{var data=await search((query.Text??"").Trim());if(at!=searchVersion)return;results.Children.Clear();foreach(var row in (data["races"] as JArray??new()).OfType<JObject>()){var id=Str(row["race_id"]);var cn=Str(row["name_cn"]);var display=Str(row["display_name"]);var original=Str(row["source_name"]);if(original.Length==0)original=Str(row["raw_name"]);var label=cn.Length>0?cn:display.Length>0?display:"未核对赛事 · "+id;var caption=new StackPanel{Spacing=3};caption.Children.Add(Note(label+" · "+id));if(original.Length>0)caption.Children.Add(Note("原文参考："+original));var check=new CheckBox{Name="LocalWinsRace_"+id,Content=caption,IsChecked=wins.Any(w=>Str(w["race_id"])==id)};check.IsCheckedChanged+=(_,_)=>{if(check.IsChecked==true){if(wins.All(w=>Str(w["race_id"])!=id))wins.Add(new JObject{["race_id"]=id,["raw_name"]=original,["status"]="confirmed",["source"]="manual"});MarkPartial();}else wins.RemoveAll(w=>Str(w["race_id"])==id);RenderSelected();};results.Children.Add(check);}if(results.Children.Count==0)results.Children.Add(Note("没有匹配；可保留实际赛事原名作为未核对记录。"));state.Text="搜索只读本地目录；勾选表示你确认这场已获胜。";}catch(InvalidOperationException e){state.Text=e.Message;}catch{state.Text="赛事目录暂不可读；已有胜场草稿未丢失。";}
    }
}
