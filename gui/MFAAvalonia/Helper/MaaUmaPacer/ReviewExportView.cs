using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Diagnostics;
using System.Globalization;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

internal sealed class ReviewExportView:UserControl
{
    private sealed record RunChoice(string Id,string Title,string Detail,string Search,DateTime? Started);
    private readonly IUmaPacerBridge bridge;
    private readonly bool continuationEnabled;
    private readonly Func<JObject>? decisionPreferences;
    private readonly Button compare=new() {Name="CompareReviewPreferences",Content="对比当前取舍参数",IsEnabled=false,Margin=new Thickness(10,0,0,0)};
    private readonly Button openComparison=new() {Name="OpenPreferencesComparison",Content="查看取舍对比",IsVisible=false,Margin=new Thickness(10,0,0,0)};
    private string? comparisonPath;
    private readonly Dictionary<string,UmaTraineeChoice> trainees;
    private readonly ListBox records=new() {Name="ReviewRuns",MinHeight=80,MaxHeight=360,HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly ComboBox scope=new() {Name="ReviewScope",Width=132,ItemsSource=new[]{"最近10局","今天","全部记录"},SelectedIndex=0};
    private readonly TextBox query=new() {Name="ReviewSearch",Watermark="搜索角色、编号或日期"};
    private readonly TextBlock count=new() {Name="ReviewCount",FontSize=13,Opacity=.75};
    private readonly Button refresh=new() {Name="RefreshReviews",Content="刷新"};
    private readonly Button export=new() {Name="ExportReview",Content="导出选中局",IsEnabled=false};
    private readonly Button open=new() {Name="OpenReviewFolder",Content="打开导出目录",IsEnabled=false};
    private readonly Button assessment=new() {Name="OpenContinuationAssessment",Content="查看续养评估（预览）",IsEnabled=false,Margin=new Thickness(10,0,0,0)};
    private readonly TextBlock status=new() {Name="ReviewStatus",Text="选择一局后即可导出。",TextWrapping=TextWrapping.Wrap};
    private readonly TextBox output=new() {Name="ReviewOutput",IsReadOnly=true,Watermark="导出文件位置"};
    private RunChoice[] allRecords=[];
    private bool busy;
    private string? exported;
    private string? assessmentPath;

    public ReviewExportView(IUmaPacerBridge bridge,IReadOnlyList<UmaTraineeChoice>? choices=null,
        ICutServiceClient? service=null,string? packageRoot=null,bool continuationEnabled=true,Func<JObject>? decisionPreferences=null)
    {
        this.bridge=bridge;
        this.continuationEnabled=continuationEnabled;assessment.IsVisible=continuationEnabled;
        this.decisionPreferences=decisionPreferences;compare.IsVisible=decisionPreferences!=null;
        trainees=(choices ?? []).GroupBy(item=>item.CardId).ToDictionary(group=>group.Key,group=>group.First());
        var body=new Grid {RowDefinitions=new("Auto,Auto,*,Auto"),RowSpacing=12,Margin=new Thickness(20)};
        var header=new StackPanel {Spacing=5};
        header.Children.Add(new TextBlock {Text="选择要导出的育成",FontSize=22});
        header.Children.Add(new TextBlock {Text="默认最近10局。搜索可查历史记录；原记录全部保留。",TextWrapping=TextWrapping.Wrap,Opacity=.75});
        body.Children.Add(header);
        var filters=new StackPanel {Spacing=6};
        var searchRow=new Grid {ColumnDefinitions=new("Auto,*,Auto"),ColumnSpacing=8};
        searchRow.Children.Add(scope);Grid.SetColumn(query,1);searchRow.Children.Add(query);
        Grid.SetColumn(refresh,2);searchRow.Children.Add(refresh);filters.Children.Add(searchRow);filters.Children.Add(count);
        Grid.SetRow(filters,1);body.Children.Add(filters);
        records.ItemTemplate=new FuncDataTemplate<RunChoice>((item,_)=>{
            if(item==null)return new TextBlock();
            var row=new StackPanel {Spacing=3,Margin=new Thickness(8,5)};
            row.Children.Add(new TextBlock {Text=item.Title,FontSize=16,TextTrimming=TextTrimming.CharacterEllipsis});
            row.Children.Add(new TextBlock {Text=item.Detail,FontSize=13,Opacity=.75,TextTrimming=TextTrimming.CharacterEllipsis});
            return row;
        });
        Grid.SetRow(records,2);body.Children.Add(records);
        var footer=new StackPanel {Spacing=8};
        var actions=new WrapPanel {Orientation=Orientation.Horizontal};
        export.Margin=new Thickness(0,0,10,0);actions.Children.Add(export);actions.Children.Add(open);actions.Children.Add(assessment);footer.Children.Add(actions);
        actions.Children.Add(compare);actions.Children.Add(openComparison);
        footer.Children.Add(status);footer.Children.Add(output);
        footer.Children.Add(new TextBlock {Text="导出训练、事件、SS、评分与耗时；如有异常停机截图，会一起放入复盘包。",FontSize=13,Opacity=.65,TextWrapping=TextWrapping.Wrap});
        CutFeedbackView? feedback=null;
        if(service!=null) {
            body.RowDefinitions=new("Auto,Auto,Auto,*");records.Height=180;
            feedback=new CutFeedbackView(service,packageRoot ?? AppContext.BaseDirectory);
            footer.Children.Add(new Expander {Name="ReviewFeedback",Header="问题反馈 · 仅维护者可见",Content=feedback,
                HorizontalContentAlignment=HorizontalAlignment.Stretch});
            // Expanded feedback remains scrollable without hiding the record list.
            var scroller=new ScrollViewer {Name="ReviewFeedbackScroll",Content=footer,MaxHeight=430};
            Grid.SetRow(scroller,3);body.Children.Add(scroller);
        } else {Grid.SetRow(footer,3);body.Children.Add(footer);}
        Content=body;
        records.SelectionChanged+=(_,_)=>{
            assessment.IsEnabled=false;assessmentPath=null;
            compare.IsEnabled=!busy && records.SelectedItem is RunChoice && decisionPreferences!=null;
            openComparison.IsVisible=false;comparisonPath=null;
            export.IsEnabled=!busy && records.SelectedItem is RunChoice;
            var choice=records.SelectedItem as RunChoice;feedback?.SetRun(choice?.Id,choice?.Title);
            if(!busy)status.Text=records.SelectedItem is RunChoice run ? "已选："+run.Title+" · "+run.Detail : "没有匹配的记录。";
        };
        query.TextChanged+=(_,_)=>ApplyFilters();
        scope.SelectionChanged+=(_,_)=>ApplyFilters();
        refresh.Click+=async(_,_)=>await Refresh();
        Loaded+=async(_,_)=>await Refresh();
        export.Click+=async(_,_)=>await Export();
        compare.Click+=async(_,_)=>await Compare();
        openComparison.Click+=(_,_)=>{if(comparisonPath!=null && File.Exists(comparisonPath))Process.Start(new ProcessStartInfo(comparisonPath){UseShellExecute=true});};
        open.Click+=(_,_)=>{
            var folder=exported is null ? null : Path.GetDirectoryName(exported);
            if(folder!=null && Directory.Exists(folder))Process.Start(new ProcessStartInfo(folder) {UseShellExecute=true});
        };
        assessment.Click+=(_,_)=>{
            if(assessmentPath is not null && File.Exists(assessmentPath))
                Process.Start(new ProcessStartInfo(assessmentPath) {UseShellExecute=true});
        };
    }

    private RunChoice Choice(JObject row)
    {
        var id=(string)row["id"]!;
        var characterId=(string?)row["character_id"] ?? "";
        trainees.TryGetValue(characterId,out var trainee);
        var name=trainee?.Label ?? (string?)row["character_name"] ?? (characterId.Length>0 ? "角色 "+characterId : "未知角色");
        DateTime? started=DateTime.TryParse((string?)row["started_at"],CultureInfo.InvariantCulture,
            DateTimeStyles.None,out var date) ? date : null;
        var title=(started?.ToString("MM-dd HH:mm") ?? "时间未知")+" · "+name;
        var state=(string?)row["status_label"] ?? "待查看";
        var detail=state+((int?)row["attempt_index"] is int index ? $" · 第{index}局" : "")+
            ((int?)row["actions_completed"] is int actions ? $" · 已行动{actions}次" : "");
        var search=title+" "+detail+" "+id+" "+characterId+" "+trainee?.Aliases+" "+started?.ToString("yyyy-MM-dd HH:mm");
        return new(id,title,detail,search,started);
    }

    private void ApplyFilters(string? preferredId=null)
    {
        var selected=preferredId ?? (records.SelectedItem as RunChoice)?.Id;
        var terms=(query.Text ?? "").Split(' ',StringSplitOptions.RemoveEmptyEntries|StringSplitOptions.TrimEntries);
        IEnumerable<RunChoice> filtered=allRecords;
        if(scope.SelectedIndex==1)filtered=filtered.Where(item=>item.Started?.Date==DateTime.Today);
        if(terms.Length>0)filtered=filtered.Where(item=>terms.All(term=>item.Search.Contains(term,StringComparison.OrdinalIgnoreCase)));
        var matches=filtered.ToArray();
        var items=scope.SelectedIndex==0 && terms.Length==0 ? matches.Take(10).ToArray() : matches;
        records.ItemsSource=items;
        records.SelectedItem=items.FirstOrDefault(item=>item.Id==selected) ?? items.FirstOrDefault();
        count.Text=terms.Length>0 ? $"匹配 {items.Length} 局"+(scope.SelectedIndex==0 ? " · 已搜索全部历史" : "") :
            $"显示 {items.Length} / {allRecords.Length} 局"+(scope.SelectedIndex==0 ? " · 较早记录可切换“全部记录”查看" : "");
        if(items.Length==0)status.Text=allRecords.Length==0 ? "还没有育成记录。开始一局后即可导出。" : "没有匹配的记录，试试其他关键词或“全部记录”。";
    }

    private void Busy(bool value)
    {
        busy=value;refresh.IsEnabled=scope.IsEnabled=query.IsEnabled=records.IsEnabled=!value;
        export.IsEnabled=!value && records.SelectedItem is RunChoice;
        compare.IsEnabled=!value && records.SelectedItem is RunChoice && decisionPreferences!=null;
    }

    private async Task Refresh()
    {
        if(busy)return;
        var selected=(records.SelectedItem as RunChoice)?.Id;Busy(true);
        try {
            var result=await bridge.Request(new JObject {["action"]="list_reviews"});
            allRecords=((JArray?)result["records"] ?? new()).OfType<JObject>()
                .Where(row=>!string.IsNullOrWhiteSpace((string?)row["id"]))
                .Select(Choice).OrderByDescending(item=>item.Started).ThenByDescending(item=>item.Id,StringComparer.Ordinal).ToArray();
            ApplyFilters(selected);
            if(records.SelectedItem is RunChoice run)status.Text="已选："+run.Title+" · "+run.Detail;
        } catch(Exception error){status.Text=error.Message;}
        finally {Busy(false);}
    }

    private async Task Compare()
    {
        if(busy || decisionPreferences==null || records.SelectedItem is not RunChoice run)return;
        Busy(true);openComparison.IsVisible=false;
        status.Text="正在用已保存现场对比本局原参数与运行面板当前参数……";
        try {
            var result=await bridge.Request(new JObject {["action"]="compare_review_preferences",["run_id"]=run.Id,["preferences"]=decisionPreferences()});
            comparisonPath=(string?)result["path"];
            openComparison.IsVisible=comparisonPath!=null && File.Exists(comparisonPath);
            status.Text=$"已比较 {result["compared"]} 份快照，{result["changed"]} 份会改选，{result["skipped"]} 份缺少完整记录。仅作单回合对比，不修改当前育成。";
        } catch(Exception error){status.Text="无法对比："+error.Message;}
        finally {Busy(false);}
    }

    private async Task Export()
    {
        if(busy || records.SelectedItem is not RunChoice run)return;
        Busy(true);open.IsEnabled=false;assessment.IsEnabled=false;assessmentPath=null;
        status.Text=continuationEnabled ? "正在整理本局文字记录与续养评估……" : "正在整理本局文字记录……";
        try {
            var result=await bridge.Request(new JObject {["action"]="export_review",["run_id"]=run.Id});
            exported=(string?)result["path"] ?? throw new InvalidOperationException("没有返回复盘包位置。");
            output.Text=exported;
            var coverage=result["coverage"];
            status.Text=$"已导出：{run.Title} · 约 {Math.Ceiling(((double?)result["size_bytes"] ?? 0)/1024):0} KB。\n"+
                $"训练记录 {coverage?["training_observations"] ?? 0} 条，事件选项 {coverage?["event_choices"] ?? 0} 条。"+
                ((bool?)result["partial_snapshot"]==true ? " 这局尚未结束，导出的是目前已有记录。" : "");
            open.IsEnabled=Directory.Exists(Path.GetDirectoryName(exported));
            assessmentPath=(string?)result["assessment_path"];
            assessment.IsEnabled=continuationEnabled && assessmentPath is not null && File.Exists(assessmentPath);
            if(assessment.IsEnabled)status.Text+="\n已附续养评估预览，仅供参考，不会改变当前运行。";
        } catch(Exception error){status.Text="导出失败："+error.Message;}
        finally {Busy(false);}
    }
}
