using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Text;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using MFAAvalonia.Helper;

namespace UmaCutPanel;

internal sealed class ScoringExchangeView:UserControl
{
    private sealed record Row(JObject Data);
    private readonly ICutServiceClient client;
    private readonly Func<JObject> currentProfile;
    private readonly Func<JObject,string,Task<bool>> use;
    private readonly Func<bool> canUse;
    private readonly Func<string> presetLabel;
    private readonly IUmaPacerBridge? reviewBridge;
    private readonly TextBox query=new() {Name="ExchangeQuery",Watermark="搜索方案名称、作者或说明",MinWidth=180};
    private readonly CheckBox mine=new() {Name="ExchangeMine",Content="只看我的方案"};
    private readonly Button refresh=new() {Name="ExchangeRefresh",Content="刷新 / 搜索"};
    private readonly Button previous=new() {Name="ExchangePrevious",Content="上一页",IsEnabled=false};
    private readonly Button next=new() {Name="ExchangeNext",Content="下一页",IsEnabled=false};
    private readonly Button publish=new() {Name="ExchangePublish",Content="发布当前权重"};
    private readonly Button edit=new() {Name="ExchangeEdit",Content="编辑我的方案",IsEnabled=false};
    private readonly Button withdraw=new() {Name="ExchangeWithdraw",Content="下架我的方案",IsEnabled=false};
    private readonly Button download=new() {Name="ExchangeDownload",Content="下载 JSON",IsEnabled=false};
    private readonly Button apply=new() {Name="ExchangeUse",Content="预览并填入配置",IsEnabled=false};
    private readonly Button compare=new() {Name="ExchangeCompare",Content="用我的复盘试算",IsEnabled=false};
    private readonly ListBox list=new() {Name="ExchangeList",MinHeight=220};
    private readonly TextBlock title=Text("选择一份方案查看详情",20);
    private readonly TextBlock details=Text("",13);
    private readonly TextBlock description=Text("",14);
    private readonly TextBlock summary=Text("",14);
    private readonly TextBlock message=Text("进入后读取群友方案；发布前可以查看将要分享的内容。",13);
    private readonly TextBlock pageLabel=Text("第1页",12);
    private readonly Border exchangeSourceBar;
    private readonly TextBlock exchangeSourceText;
    private readonly Button exchangeRestoreOfficial;
    private Func<(ProvenanceEntry? Entry, string? CurrentRevision)>? readSourceInfo;
    private Func<string?>? getCurrentRevision;
    private Action<JObject>? schemeAppliedCallback;
    private Action? restoreOfficialCallback;
    private JObject? selected,publishDraft;
    private string? draftId,pendingBody,pendingRequest;
    private bool busy,loaded,changingFilters;
    private int offset;
    private int? nextOffset;

    internal ScoringExchangeView(ICutServiceClient client,Func<JObject> currentProfile,
        Func<JObject,string,Task<bool>> use,Func<bool> canUse,Func<string> presetLabel,IUmaPacerBridge? reviewBridge=null)
    {
        this.client=client;this.currentProfile=currentProfile;this.use=use;this.canUse=canUse;this.presetLabel=presetLabel;
        this.reviewBridge=reviewBridge;compare.IsVisible=reviewBridge!=null;
        var root=new Grid {RowDefinitions=new("Auto,Auto,*,Auto"),Margin=new Thickness(20),RowSpacing=12};
        var header=new StackPanel {Spacing=6};header.Children.Add(Text("权重交换站",22));
        header.Children.Add(Text("已激活群友可以分享评分设置和公式。复盘仍然只有维护者可见。下载不会改变当前配置，填入后需保存，新一轮生效。",13));
        exchangeSourceText=Text("",13);
        exchangeSourceText.VerticalAlignment=VerticalAlignment.Center;
        exchangeRestoreOfficial=new Button {Name="ExchangeRestoreOfficial",Content="✦ 停用外部方案并恢复官方默认",Height=30,Padding=new Thickness(10,4),Margin=new Thickness(8,0,0,0)};
        var sourceRow=new DockPanel();
        DockPanel.SetDock(exchangeRestoreOfficial,Dock.Right);
        sourceRow.Children.Add(exchangeRestoreOfficial);
        sourceRow.Children.Add(exchangeSourceText);
        exchangeSourceBar=new Border {Name="ExchangeSourceBar",IsVisible=false,BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(8),Padding=new Thickness(10,8),Child=sourceRow};
        exchangeRestoreOfficial.Click+=(_,_)=>{
            restoreOfficialCallback?.Invoke();
            RefreshSourceBar();
            message.Text="已停用外部方案并恢复官方纯净默认值；请点顶栏“保存配置”提交。";
        };
        header.Children.Add(exchangeSourceBar);
        root.Children.Add(header);
        var tools=new WrapPanel {Orientation=Orientation.Horizontal};
        foreach(var item in new Control[]{query,refresh,mine,publish}){item.Margin=new Thickness(0,0,10,6);tools.Children.Add(item);}
        Grid.SetRow(tools,1);root.Children.Add(tools);
        var body=new Grid {ColumnDefinitions=new("280,*"),ColumnSpacing=18};
        list.ItemTemplate=new FuncDataTemplate<Row>((row,_)=>{
            var item=new StackPanel {Spacing=4,Margin=new Thickness(4)};
            var (_,baseFamily,_)=PacerUiProvenanceStore.InferBaseline(row?.Data);
            var curRev=getCurrentRevision?.Invoke();
            var curFamily=!string.IsNullOrWhiteSpace(curRev)?PacerPurity.FamilyOf(curRev):null;
            var badgeText=string.IsNullOrWhiteSpace(baseFamily)?"? 基线未知":curFamily!=null&&baseFamily==curFamily?$"✓ {baseFamily}适配":$"△ {baseFamily}基线";
            item.Children.Add(Text((string?)row?.Data["title"]??"未命名方案",15));
            item.Children.Add(Text($"{row?.Data["author_name"]} · 第{row?.Data["revision"]}版 · {Status((string?)row?.Data["status"])} · {badgeText}",12));return item;
        });
        var left=new Grid {RowDefinitions=new("*,Auto")};left.Children.Add(list);
        var pages=new StackPanel {Orientation=Orientation.Horizontal,Spacing=8,Margin=new Thickness(0,8,0,0)};
        pages.Children.Add(previous);pages.Children.Add(pageLabel);pages.Children.Add(next);Grid.SetRow(pages,1);left.Children.Add(pages);body.Children.Add(left);
        var right=new StackPanel {Spacing=12};right.Children.Add(title);right.Children.Add(details);right.Children.Add(description);right.Children.Add(summary);
        var buttons=new WrapPanel {Orientation=Orientation.Horizontal};
        foreach(var button in new[]{compare,apply,download,edit,withdraw}){button.Margin=new Thickness(0,0,8,8);buttons.Children.Add(button);}right.Children.Add(buttons);
        right.Children.Add(Text("运行中可以浏览和下载；结束本轮后再填入配置。方案只包含评分取舍，不会带入他人的账号、连接、卡组或消耗额度。",12));
        var scroll=new ScrollViewer {Content=right,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled};
        Grid.SetColumn(scroll,1);body.Children.Add(scroll);Grid.SetRow(body,2);root.Children.Add(body);
        message.Name="ExchangeMessage";Grid.SetRow(message,3);root.Children.Add(message);Content=root;
        Loaded+=async(_,_)=>{if(!loaded){loaded=true;await Reload(0);}};
        refresh.Click+=async(_,_)=>await Reload(0);
        query.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter){e.Handled=true;await Reload(0);}};
        mine.IsCheckedChanged+=async(_,_)=>{if(loaded&&!busy&&!changingFilters)await Reload(0);};
        previous.Click+=async(_,_)=>await Reload(Math.Max(0,offset-40));
        next.Click+=async(_,_)=>{if(nextOffset is int value)await Reload(value);};
        list.SelectionChanged+=async(_,_)=>{if(!busy)await Work(LoadSelected);};
        publish.Click+=async(_,_)=>await Publish(false);
        edit.Click+=async(_,_)=>await Publish(true);
        withdraw.Click+=async(_,_)=>await Withdraw();
        download.Click+=async(_,_)=>await Download();
        apply.Click+=async(_,_)=>await Use();
        compare.Click+=async(_,_)=>{
            if(busy||reviewBridge==null||selected?["profile"] is not JObject profile||TopLevel.GetTopLevel(this) is not Window owner)return;
            var dialog=new ScoringComparisonWindow(reviewBridge,(JObject)profile.DeepClone(),(string?)selected["title"]??"群友方案");
            await dialog.ShowDialog(owner);
        };
    }
    private static TextBlock Text(string text,double size)=>new(){Text=text,FontSize=size,TextWrapping=TextWrapping.Wrap};
    private static string Status(string? value)=>value switch {"published"=>"已发布","withdrawn"=>"已下架","blocked"=>"维护者已下架",_=>"状态待确认"};
    internal static string Summary(JObject profile)
    {
        var p=profile["preferences"] as JObject??new();
        var text=$"数值收益 {(double?)p["numeric"]??1:P0} · 羁绊 {(double?)p["bond"]??1:P0} · 充电 {(double?)p["charge"]??1:P0}\n黄金船额外分 {p["gold_ship_bonus"]??0} · 黄帽额外分 {p["mei_bonus"]??0}";
        var tuning=p["tuning"] as JObject;var mode=(string?)tuning?["mode"];
        if(mode=="formula")text+="\n评分公式：\n"+(string?)tuning?["formula"];
        else if(mode=="detailed")text+=$"\n详细倍率：已调整 {(tuning?["factors"] as JObject)?.Count??0} 项；可在预览中逐项查看。";
        else text+="\n高级评分：保持原策略。";
        return text;
    }
    private void Buttons()
    {
        query.IsEnabled=refresh.IsEnabled=mine.IsEnabled=publish.IsEnabled=list.IsEnabled=!busy;
        previous.IsEnabled=!busy&&offset>0;next.IsEnabled=!busy&&nextOffset!=null;
        var own=(bool?)selected?["mine"]==true&& (string?)selected?["status"]!="blocked";
        edit.IsEnabled=withdraw.IsEnabled=!busy&&own;
        download.IsEnabled=apply.IsEnabled=!busy&&selected?["profile"] is JObject;
        compare.IsEnabled=!busy&&reviewBridge!=null&&selected?["profile"] is JObject;
    }
    private async Task<JObject> Request(JObject request)
    {
        var result=await client.Request(request);
        if((bool?)result["ok"]!=true)throw new InvalidOperationException((string?)result["message"]??"交换站操作未完成。");
        return result;
    }
    private async Task Work(Func<Task> work)
    {
        if(busy)return;busy=true;Buttons();
        try{await work();}catch(Exception error){message.Text=error.Message;}
        finally{busy=false;Buttons();}
    }
    private Task Reload(int page)=>Work(async()=>{
        message.Text="正在读取群友方案……";
        var result=await Request(new JObject {["action"]="scoring_list",["query"]=query.Text??"",["mine"]=mine.IsChecked==true,["offset"]=page});
        var rows=(result["profiles"] as JArray??new()).OfType<JObject>().Select(item=>new Row(item)).ToArray();
        offset=page;nextOffset=(int?)result["next_offset"];pageLabel.Text=$"第{offset/40+1}页";
        list.ItemsSource=rows;list.SelectedIndex=rows.Length>0?0:-1;selected=null;
        if(rows.Length>0){await LoadSelected();message.Text=$"本页 {rows.Length} 份方案，选择后可预览和下载。";}
        else{title.Text="暂无匹配方案";details.Text=description.Text=summary.Text="";message.Text="可以发布第一份方案，或换个关键词查找。";}
    });
    private async Task LoadSelected()
    {
        selected=null;details.Text=description.Text=summary.Text="";
        if(list.SelectedItem is not Row row){title.Text="选择一份方案查看详情";return;}
        title.Text=(string?)row.Data["title"];
        var result=await Request(new JObject {["action"]="scoring_get",["id"]=row.Data["id"]});
        selected=(JObject)result["scheme"]!;
        title.Text=(string?)selected["title"];
        details.Text=$"作者：{selected["author_name"]} · 第{selected["revision"]}版 · {Status((string?)selected["status"])}\n作者使用版本：{selected["app_version"]}";
        details.Text+=$"\n评分语义：{selected["profile"]?["scoring_semantics"]?["model"]} / {selected["profile"]?["scoring_semantics"]?["version"]}";
        var (baseLabel,baseFamily,_)=PacerUiProvenanceStore.InferBaseline(selected);
        details.Text+=$"\n策略基线：{baseLabel}";
        var curRev=getCurrentRevision?.Invoke();
        if(!string.IsNullOrWhiteSpace(curRev)&&baseFamily!=null&&baseFamily!=PacerPurity.FamilyOf(curRev))
            details.Text+=$"\n⚠ 跨代提示：此方案基于 {baseLabel}，而当前配置采用 {curRev} 策略（不同族调参体系，直接覆盖可能导致平衡异常）。";
        description.Text=(string?)selected["description"];summary.Text=Summary((JObject)selected["profile"]!);
    }
    internal void BindWorkflowProvenance(
        Func<(ProvenanceEntry? Entry, string? CurrentRevision)> readSource,
        Func<string?> currentRevision,
        Action<JObject> onSchemeApplied,
        Action onRestoreOfficial)
    {
        readSourceInfo=readSource;
        getCurrentRevision=currentRevision;
        schemeAppliedCallback=onSchemeApplied;
        restoreOfficialCallback=onRestoreOfficial;
        RefreshSourceBar();
    }
    internal void RefreshSourceBar()
    {
        if(readSourceInfo==null){exchangeSourceBar.IsVisible=false;return;}
        var (entry,curRev)=readSourceInfo();
        if(entry==null||entry.Source!="exchange_scheme"){exchangeSourceBar.IsVisible=false;return;}
        var schemeBase=entry.BaselineRevision??"未知";
        var mismatch=!string.IsNullOrWhiteSpace(curRev)
            &&!string.IsNullOrWhiteSpace(entry.BaselineRevision)
            &&!PacerPurity.SameFamily(curRev,entry.BaselineRevision);
        exchangeSourceText.Text=mismatch
            ?$"🔴 当前配置正应用外部方案「{entry.Title??entry.SchemeId}」（原基线 {schemeBase} ≠ 当前策略 {curRev}，已跨代叠加）"
            :$"🟠 当前配置正应用外部方案「{entry.Title??entry.SchemeId}」（基线：{schemeBase} · 导入于 {entry.AppliedAt}）";
        exchangeSourceBar.BorderBrush=new SolidColorBrush(mismatch?Color.FromRgb(220,38,38):Color.FromRgb(217,119,6));
        exchangeSourceBar.Background=new SolidColorBrush(mismatch?Color.FromArgb(24,239,68,68):Color.FromArgb(20,245,158,11));
        exchangeSourceBar.IsVisible=true;
    }
    private async Task Publish(bool updating)
    {
        if(busy||TopLevel.GetTopLevel(this) is not Window owner)return;
        if(updating&&(bool?)selected?["mine"]!=true){message.Text="请选择自己发布的方案。";return;}
        var existing=updating?(JObject)selected!.DeepClone():null;
        var id=(string?)existing?["id"];
        var draft=draftId==id?publishDraft:null;
        var dialog=new ScoringPublishWindow(currentProfile(),existing,draft,presetLabel());
        var body=await dialog.ShowDialog<JObject?>(owner);if(body==null)return;
        publishDraft=(JObject)body.DeepClone();draftId=id;
        body["action"]="scoring_publish";
        if(getCurrentRevision?.Invoke() is {Length:>0} curRev)body["baseline_revision"]=curRev;
        if(existing!=null){body["id"]=existing["id"];body["expected_revision"]=existing["revision"];}
        Identify(body);
        var succeeded=false;
        await Work(async()=>{
            message.Text="正在发布方案……";var result=await Request(body);succeeded=true;
            pendingBody=pendingRequest=null;publishDraft=null;draftId=null;
            message.Text=(string?)result["message"]??"方案已发布。";
        });
        if(succeeded){changingFilters=true;query.Text="";mine.IsChecked=true;changingFilters=false;await Reload(0);}
    }
    private void Identify(JObject body)
    {
        var identity=body.ToString(Formatting.None);
        if(pendingBody!=identity){pendingBody=identity;pendingRequest=Guid.NewGuid().ToString("N");}
        body["request_id"]=pendingRequest;
    }
    private async Task Withdraw()
    {
        if(busy||selected==null||(bool?)selected["mine"]!=true||TopLevel.GetTopLevel(this) is not Window owner)return;
        var row=(JObject)selected.DeepClone();
        var confirm=new Window {Title="下架我的方案",Width=460,SizeToContent=SizeToContent.Height,CanResize=false,WindowStartupLocation=WindowStartupLocation.CenterOwner};
        var body=new StackPanel {Margin=new Thickness(22),Spacing=14};
        body.Children.Add(Text($"下架“{row["title"]}”后，群友列表将不再显示。已经下载的文件仍在对方本机保留。",14));
        var buttons=new StackPanel {Orientation=Orientation.Horizontal,Spacing=10};
        var cancel=new Button {Name="ExchangeWithdrawCancel",Content="取消"};var yes=new Button {Name="ExchangeWithdrawConfirm",Content="确认下架"};
        cancel.Click+=(_,_)=>confirm.Close(false);yes.Click+=(_,_)=>confirm.Close(true);buttons.Children.Add(cancel);buttons.Children.Add(yes);body.Children.Add(buttons);confirm.Content=body;
        if(!await confirm.ShowDialog<bool>(owner))return;
        var succeeded=false;
        var request=new JObject {["action"]="scoring_withdraw",["id"]=row["id"],["expected_revision"]=row["revision"]};Identify(request);
        await Work(async()=>{await Request(request);succeeded=true;pendingBody=pendingRequest=null;});
        if(succeeded)await Reload(0);
    }
    private async Task Use()
    {
        if(busy||selected?["profile"] is not JObject profile)return;
        if(!canUse()){message.Text="请先结束本轮，再把方案填入配置；运行中仍可浏览和下载。";return;}
        var copy=(JObject)profile.DeepClone();var label=(string?)selected["title"]??"群友方案";
        var (baseLabel,baseFamily,_)=PacerUiProvenanceStore.InferBaseline(selected);
        var curRev=getCurrentRevision?.Invoke();
        if(!string.IsNullOrWhiteSpace(curRev)&&baseFamily!=null&&baseFamily!=PacerPurity.FamilyOf(curRev))
            label+=$" [⚠ 跨代基线：{baseLabel} vs 当前 {curRev}]";
        var appliedScheme=(JObject)selected.DeepClone();
        await Work(async()=>{if(await use(copy,label)){schemeAppliedCallback?.Invoke(appliedScheme);RefreshSourceBar();message.Text=$"已填入{presetLabel()}，请点击上方“保存配置”。新一轮生效。";}});
    }
    private async Task Download()
    {
        if(busy||selected?["profile"] is not JObject profile||TopLevel.GetTopLevel(this) is not Window owner)return;
        var value=(JObject)profile.DeepClone();var label=(string?)selected["title"]??"群友方案";
        var safe=new string(label.Select(c=>Path.GetInvalidFileNameChars().Contains(c)?'_':c).ToArray());
        await Work(async()=>{
            var file=await owner.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions {Title="下载权重方案",SuggestedFileName="MaaUma-权重-"+safe+".json",DefaultExtension="json",FileTypeChoices=new[]{new FilePickerFileType("JSON评分配置"){Patterns=new[]{"*.json"}}}});
            if(file==null)return;
            await using var stream=await file.OpenWriteAsync();stream.SetLength(0);
            await using var writer=new StreamWriter(stream,new UTF8Encoding(false));await writer.WriteAsync(value.ToString(Formatting.Indented));
            message.Text="已下载纯评分配置，可在“自写公式／导入配置”中使用。";
        });
    }
}

internal sealed class ScoringPublishWindow:Window
{
    internal ScoringPublishWindow(JObject current,JObject? existing,JObject? draft,string preset)
    {
        Title=existing==null?"发布当前权重":"编辑我的方案";Width=650;Height=650;MinWidth=500;MinHeight=480;WindowStartupLocation=WindowStartupLocation.CenterOwner;
        var title=new TextBox {Name="ExchangeFormTitle",Text=(string?)(draft?["title"]??existing?["title"]),MaxLength=60,Watermark="例如：黄金船与黄帽开局取舍"};
        var author=new TextBox {Name="ExchangeFormAuthor",Text=(string?)(draft?["author_name"]??existing?["author_name"]),MaxLength=40,Watermark="群友能看到的昵称"};
        var description=new TextBox {Name="ExchangeFormDescription",Text=(string?)(draft?["description"]??existing?["description"]),MaxLength=2000,AcceptsReturn=true,MinHeight=95,TextWrapping=TextWrapping.Wrap,Watermark="建议说明适用角色、卡组、阶段和调整思路"};
        current=(JObject)current.DeepClone();
        var saved=(JObject?)(draft?["profile"]??existing?["profile"]);
        var initial=(JObject)(saved??current).DeepClone();
        var replace=new CheckBox {Name="ExchangeFormUseCurrent",Content=$"改用面板当前{preset}的权重",IsVisible=saved!=null,IsChecked=false};
        var summary=new SelectableTextBlock {Name="ExchangeFormSummary",Text=ScoringExchangeView.Summary(initial),TextWrapping=TextWrapping.Wrap};
        var message=new TextBlock {Name="ExchangeFormMessage",TextWrapping=TextWrapping.Wrap};
        JObject Selected()=>replace.IsChecked==true?current:initial;
        replace.IsCheckedChanged+=(_,_)=>summary.Text=ScoringExchangeView.Summary(Selected());
        var content=new StackPanel {Margin=new Thickness(24),Spacing=10};
        content.Children.Add(new TextBlock {Text="发布后，已激活群友能看到名称、昵称、说明及评分配置。请检查下方内容，再确认分享。",TextWrapping=TextWrapping.Wrap});
        foreach(var (caption,control) in new (string,Control)[]{("方案名称",title),("公开昵称",author),("适用范围与说明",description)}){content.Children.Add(new TextBlock {Text=caption});content.Children.Add(control);}
        content.Children.Add(replace);content.Children.Add(summary);content.Children.Add(message);
        var buttons=new StackPanel {Orientation=Orientation.Horizontal,Spacing=10};
        var cancel=new Button {Content="取消",Name="ExchangeFormCancel"};var submit=new Button {Content=existing==null?"确认发布":"保存并发布修改",Name="ExchangeFormSubmit"};
        cancel.Click+=(_,_)=>Close(null);
        submit.Click+=(_,_)=>{
            if(string.IsNullOrWhiteSpace(title.Text)||string.IsNullOrWhiteSpace(author.Text)){message.Text="请填写方案名称和公开昵称。";return;}
            Close(new JObject {["title"]=title.Text.Trim(),["author_name"]=author.Text.Trim(),["description"]=description.Text??"",["profile"]=Selected().DeepClone()});
        };
        buttons.Children.Add(cancel);buttons.Children.Add(submit);content.Children.Add(buttons);
        Content=new ScrollViewer {Content=content,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled};
    }
}
