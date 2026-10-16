using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Text;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Interactivity;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

/// <summary>Shared local library page. The host supplies a data-only request function.</summary>
public sealed class UmaParentLibraryView : UserControl
{
    private sealed record CatalogChoice(string? Key,string Kind,string Name){public override string ToString()=>Name;}
    private readonly Func<JObject,Task<JObject>> request;
    private readonly bool explicitDeviceSelection;
    private readonly ComboBox devices=new(){Name="ParentLibraryDevices",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly Button refreshDevices=new(){Name="ParentLibraryRefreshDevices",Content="刷新设备"};
    private readonly TextBlock selectedTarget=Text("请选择本次扫描设备",13);
    private readonly TextBlock actualTarget=Text("",13);
    private readonly TextBlock targetPath=Text("尚未选择设备",12);
    private bool devicesLoaded,loadingDevices;
    private readonly TextBox search=new(){Name="ParentLibrarySearch",Watermark="关键词：角色、衣装、因子原文或备注"};
    private readonly TextBox factorSearch=new(){Name="ParentLibraryFactorSearch",Watermark="添加因子，逗号分隔；输入部分名称可选候选"};
    private readonly ComboBox factorMode=new(){Name="ParentLibraryFactorMode",ItemsSource=new[]{"全部满足","任意满足"},SelectedIndex=0,Width=130};
    private readonly WrapPanel factorChips=new(){Name="ParentLibraryFactorChips",Orientation=Orientation.Horizontal};
    private readonly ListBox factorSuggestions=new(){Name="ParentLibraryFactorSuggestions",MaxHeight=125,IsVisible=false};
    private readonly TextBlock factorHint=Text("可先输入名称，再从目录候选选择；不把相似名称自动当作命中。",12);
    private readonly List<JObject> selectedFactors=new();
    private JObject[] factorCatalog=[];
    private readonly HashSet<string> scenarioNames=new(StringComparer.Ordinal);
    private readonly HashSet<string> observedRawNames=new(StringComparer.Ordinal);
    private bool factorCatalogLoaded,editingFactorText;
    private readonly List<Bitmap> portraitBitmaps=new();
    private readonly Button refresh=new(){Name="ParentLibraryRefresh",Content="搜索 / 刷新"};
    private readonly Button import=new(){Name="ParentLibraryImport",Content="导入并替换"};
    private readonly Button export=new(){Name="ParentLibraryExport",Content="导出本地库"};
    private readonly Button planner=new(){Name="ParentLibraryPlanner",Content="导出规划快照"};
    private readonly Button scanStart=new(){Name="ParentLibraryScanStart",Content="扫描种马库",IsEnabled=false};
    private readonly Button scanStop=new(){Name="ParentLibraryScanStop",Content="停止扫描并保存",IsEnabled=false};
    private readonly Button scanRefresh=new(){Name="ParentLibraryScanRefresh",Content="刷新进度"};
    private readonly TextBox scanLimit=new(){Name="ParentLibraryScanLimit",Watermark="留空扫描全库",Width=150};
    private readonly DispatcherTimer scanTimer=new(){Interval=TimeSpan.FromSeconds(1)};
    private readonly ListBox entries=new(){Name="ParentLibraryEntries",MinHeight=130};
    private readonly StackPanel details=new(){Name="ParentLibraryDetails",Spacing=9};
    private readonly TextBlock status=Text("读取本地种马库…",13);
    private readonly TextBlock scan=Text("扫描页面待校准",13);
    private readonly TextBlock count=Text("",13);
    private readonly Grid columns=new(){ColumnDefinitions=new("280,*"),ColumnSpacing=16};
    private readonly ScrollViewer detailScroll;
    private readonly Grid listPane;
    private JObject? selected;
    private bool busy,selecting;
    private int selectionGeneration;
    private int? revision;
    private bool scanAvailable,scanRunning,scanPolling;
    private string? scanId;

    public UmaParentLibraryView(Func<JObject,Task<JObject>> request,bool explicitDeviceSelection=false)
    {
        this.request=request;
        this.explicitDeviceSelection=explicitDeviceSelection;
        status.Name="ParentLibraryStatus";scan.Name="ParentLibraryScanStatus";count.Name="ParentLibraryCount";
        selectedTarget.Name="ParentLibraryTargetSummary";actualTarget.Name="ParentLibraryActualTarget";targetPath.Name="ParentLibraryTargetPath";
        var root=new Grid {RowDefinitions=new("Auto,Auto,*,Auto"),Margin=new Thickness(16),RowSpacing=7};
        var header=new StackPanel{Spacing=3};
        var heading=new Grid{ColumnDefinitions=new("Auto,*"),ColumnSpacing=12};heading.Children.Add(Text("种马库",22));
        var introduction=Text("共用本机库 · 每匹成品分别记录",12);introduction.VerticalAlignment=VerticalAlignment.Center;Grid.SetColumn(introduction,1);heading.Children.Add(introduction);header.Children.Add(heading);
        var devicePanel=new StackPanel{Spacing=2,IsVisible=explicitDeviceSelection};
        var deviceRow=new Grid{ColumnDefinitions=new("Auto,*,Auto"),ColumnSpacing=8};
        var deviceTitle=Text("扫描设备",12);deviceTitle.VerticalAlignment=VerticalAlignment.Center;deviceRow.Children.Add(deviceTitle);
        Grid.SetColumn(devices,1);deviceRow.Children.Add(devices);Grid.SetColumn(refreshDevices,2);deviceRow.Children.Add(refreshDevices);
        devices.ItemTemplate=new FuncDataTemplate<JObject>((item,_)=>Text(DeviceLabel(item),13));
        devicePanel.Children.Add(deviceRow);devicePanel.Children.Add(selectedTarget);header.Children.Add(devicePanel);
        actualTarget.IsVisible=false;header.Children.Add(actualTarget);
        var scanControls=new WrapPanel{Orientation=Orientation.Horizontal};
        foreach(var control in new Control[]{scanStart,scanStop,scanRefresh,scanLimit}){control.Margin=new Thickness(0,0,8,3);scanControls.Children.Add(control);}
        ToolTip.SetTip(scanLimit,"本次最多扫描多少匹（1～2000）；留空按全库扫描。");
        var scanBody=new StackPanel{Spacing=4};scanBody.Children.Add(scanControls);
        scanBody.Children.Add(Text("开始前停在名人堂列表，关闭筛选并回到顶部。只读取列表与因子，不切号或消费。",12));
        if(explicitDeviceSelection)scanBody.Children.Add(targetPath);
        var scanHeading=new Grid{ColumnDefinitions=new("Auto,*"),ColumnSpacing=10};scanHeading.Children.Add(Text("扫描操作",13));Grid.SetColumn(scan,1);scanHeading.Children.Add(scan);
        header.Children.Add(new Expander{Name="ParentLibraryScanExpander",Header=scanHeading,Content=scanBody,IsExpanded=false,HorizontalAlignment=HorizontalAlignment.Stretch});
        root.Children.Add(header);
        var toolbar=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=8};toolbar.Children.Add(search);
        Grid.SetColumn(refresh,1);toolbar.Children.Add(refresh);
        var filters=new StackPanel{Spacing=3};filters.Children.Add(toolbar);
        var factorRow=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=8};factorRow.Children.Add(factorSearch);Grid.SetColumn(factorMode,1);factorRow.Children.Add(factorMode);
        filters.Children.Add(factorRow);filters.Children.Add(factorChips);filters.Children.Add(factorHint);filters.Children.Add(factorSuggestions);
        factorSuggestions.ItemTemplate=new FuncDataTemplate<JObject>((item,_)=>Text((bool?)item?["is_raw"]==true?
            "按原文匹配："+Value(item,"raw_name",""):Value(item,"name","未知因子")+" · "+Kind((string?)item?["kind"]),13));
        Grid.SetRow(filters,1);root.Children.Add(filters);
        entries.ItemTemplate=new FuncDataTemplate<JObject>((row,_)=> {
            var text=new StackPanel{Spacing=4,Margin=new Thickness(6,8)};
            text.Children.Add(Text(EntryTitle(row),16));
            text.Children.Add(Text("成品编号："+Value(row,"entry_id","未记录"),11));
            if(!string.IsNullOrWhiteSpace(Value(row,"nickname","")))text.Children.Add(Text(Value(row,"nickname",""),12));
            if(row?["assessment"] is JObject assessed)text.Children.Add(Text(Value(assessed,"status","")=="known_satisfied"?"符合条件":Value(assessed,"label","可用程度待评估"),12));
            return text;
        });
        listPane=new Grid{RowDefinitions=new("Auto,*"),RowSpacing=5};listPane.Children.Add(count);Grid.SetRow(entries,1);listPane.Children.Add(entries);
        columns.Children.Add(Card(listPane));
        detailScroll=new ScrollViewer{Name="ParentLibraryDetailScroll",Content=details,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled};
        Grid.SetColumn(detailScroll,1);columns.Children.Add(detailScroll);Grid.SetRow(columns,2);root.Children.Add(columns);
        var footer=new StackPanel{Spacing=3};var buttons=new WrapPanel{Orientation=Orientation.Horizontal};
        foreach(var button in new[]{import,export,planner}){button.Margin=new Thickness(0,0,8,4);buttons.Children.Add(button);}
        footer.Children.Add(buttons);footer.Children.Add(Text("导入会替换共享库，请先导出需要保留的记录。",12));footer.Children.Add(status);Grid.SetRow(footer,3);root.Children.Add(footer);Content=root;
        ShowEmpty();
        refresh.Click+=async(_,_)=>await ApplyFilters();
        search.KeyDown+=async(_,args)=>{if(args.Key==Avalonia.Input.Key.Enter){args.Handled=true;await ApplyFilters();}};
        factorSearch.TextChanged+=(_,_)=>{if(!editingFactorText)UpdateFactorSuggestions();};
        factorSearch.KeyDown+=async(_,args)=>{if(args.Key==Avalonia.Input.Key.Enter){args.Handled=true;await ApplyFilters();}};
        factorSuggestions.SelectionChanged+=async(_,_)=>{
            if(factorSuggestions.SelectedItem is not JObject row)return;
            AddFactorChip(row);var tokens=ParentLibraryUiLogic.Tokens(factorSearch.Text);SetFactorText(string.Join("，",tokens.Skip(1)));
            if(ParentLibraryUiLogic.Tokens(factorSearch.Text).Length==0)await Refresh();
        };
        factorMode.SelectionChanged+=async(_,_)=>{if(revision.HasValue&&!busy)await Refresh();};
        entries.SelectionChanged+=async(_,_)=>{if(!selecting)await LoadSelection();};
        import.Click+=async(_,_)=>await ImportFile();export.Click+=async(_,_)=>await ExportFile(false);planner.Click+=async(_,_)=>await ExportFile(true);
        scanStart.Click+=async(_,_)=>await ConfirmScan();scanStop.Click+=async(_,_)=>await StopScan();scanRefresh.Click+=async(_,_)=>await RefreshScan();
        refreshDevices.Click+=async(_,_)=>await RefreshDevices(true);
        devices.SelectionChanged+=(_,_)=>{UpdateSelectedTarget();Lock(busy);};
        scanTimer.Tick+=async(_,_)=>{if(scanRunning)await RefreshScan();};
        Loaded+=async(_,_)=>{scanTimer.Start();await Refresh();};Unloaded+=(_,_)=>scanTimer.Stop();SizeChanged+=(_,_)=>Reflow();
    }

    private static TextBlock Text(string text,int size=14)=>new(){Text=text,FontSize=size,TextWrapping=TextWrapping.Wrap};
    private static string Value(JObject? obj,string key,string fallback)=>obj?[key]?.Type==JTokenType.String?(string?)obj[key]??fallback:fallback;
    private static string EntryTitle(JObject? row)=>string.Join(" · ",new[]{Value(row,"character_name","未识别角色"),Value(row,"variant_name","")}.Where(x=>x.Length>0));
    private static string DeviceLabel(JObject? value)=>value==null?"请选择本次扫描设备":
        Value(value,"name","本机模拟器")+" · "+Value(value,"endpoint","端口未配置")+((bool?)value["busy"]==true?"（任务运行中）":"");
    private void UpdateSelectedTarget()
    {
        var selected=devices.SelectedItem as JObject;
        selectedTarget.Text=selected==null?"请选择本次扫描设备；没有看到目标时点“刷新设备”。":"本次选择："+DeviceLabel(selected);
        targetPath.Text=selected==null?"尚未选择设备":"ADB："+Value(selected,"adb_path","未配置")+"\n来源："+Value(selected,"source","面板设备列表");
    }
    public async Task RefreshDevices(bool discover=false)
    {
        if(!explicitDeviceSelection||loadingDevices||scanRunning)return;
        var prior=Value(devices.SelectedItem as JObject,"id","");loadingDevices=true;Lock(busy);
        try{
            var response=await request(new JObject{["action"]="parent_library_devices",["refresh"]=discover});
            var choices=(response["devices"] as JArray??new()).OfType<JObject>().ToArray();
            devices.ItemsSource=choices;
            var selectedId=prior.Length>0?prior:Value(response,"selected_id","");
            devices.SelectedItem=choices.FirstOrDefault(x=>Value(x,"id","")==selectedId&&selectedId.Length>0);
            devicesLoaded=true;UpdateSelectedTarget();
            if(choices.Length==0)selectedTarget.Text="未找到已配置的模拟器，请先配置设备或点“刷新设备”。";
        }catch(InvalidOperationException error){selectedTarget.Text=error.Message;}
        catch(Exception){selectedTarget.Text="设备列表暂未读取成功，请刷新设备后再试。";}
        finally{loadingDevices=false;Lock(busy);}
    }
    private static GlassCard Card(Control content)=>new(){Content=content,Padding=new Thickness(12),IsAnimated=false,IsOpaque=true,CornerRadius=new CornerRadius(12)};
    private static string Kind(string? kind)=>kind switch{"blue"=>"蓝因子","red"=>"红因子","white"=>"白因子","green"=>"绿因子",_=>"类型待校对"};
    private static string Stars(JToken? value)=>value?.Type==JTokenType.Integer&&value.Value<int>() is >=1 and <=3?new string('★',value.Value<int>()):"星级未识别";

    private void Reflow()
    {
        var narrow=Bounds.Width<650;
        columns.ColumnDefinitions=new(narrow?"*":Bounds.Width<950?"210,*":"250,*");columns.RowDefinitions=new(narrow?"Auto,*":"*");
        Grid.SetColumn(detailScroll,narrow?0:1);Grid.SetRow(detailScroll,narrow?1:0);
        detailScroll.Margin=narrow?new Thickness(0,12,0,0):new Thickness(0);
        entries.MinHeight=narrow?80:130;entries.MaxHeight=narrow?105:double.PositiveInfinity;
    }
    private void Lock(bool value)
    {
        busy=value;refresh.IsEnabled=entries.IsEnabled=!value;
        import.IsEnabled=!value&&revision.HasValue&&!scanRunning;
        export.IsEnabled=planner.IsEnabled=!value&&revision.HasValue;
        details.IsEnabled=!value&&!scanRunning;
        scanStart.IsEnabled=!value&&!scanPolling&&!loadingDevices&&scanAvailable&&!scanRunning&&(!explicitDeviceSelection||devices.SelectedItem is JObject);scanLimit.IsEnabled=!value&&!scanRunning;
        devices.IsEnabled=refreshDevices.IsEnabled=!value&&!loadingDevices&&!scanRunning;
        factorSearch.IsEnabled=factorMode.IsEnabled=factorChips.IsEnabled=factorSuggestions.IsEnabled=!value;
        scanStop.IsEnabled=!value&&!scanPolling&&scanRunning&&!string.IsNullOrEmpty(scanId);scanRefresh.IsEnabled=!value&&!scanPolling;
    }
    private void ClearDetails(){details.Children.Clear();foreach(var image in portraitBitmaps)image.Dispose();portraitBitmaps.Clear();}
    private void ShowEmpty(){ClearDetails();details.Children.Add(Card(Text("选择一匹成品查看本体与两名亲代的因子。未读到的信息会保留为未知，不会算作没有。")));}
    private async Task EnsureFactorCatalog()
    {
        if(factorCatalogLoaded)return;
        try{
            var response=await request(new JObject{["action"]="parent_library_catalog"});
            factorCatalog=(response["factors"] as JArray??new()).OfType<JObject>().Where(x=>x["factor_key"]?.Type==JTokenType.String).ToArray();
            scenarioNames.Clear();foreach(var name in (response["scenario_names"] as JArray??new()).Values<string>())if(name!=null)scenarioNames.Add(name);
            factorCatalogLoaded=true;UpdateFactorSuggestions();
        }catch{factorHint.Text="因子目录暂未读取，可先用关键词查询；不会猜配目录项。";}
    }
    private JObject FilterRequest(string action)=>new(){["action"]=action,["scope"]="pedigree",
        ["match_mode"]=factorMode.SelectedIndex==1?"any":"all",
        ["requirements"]=new JArray(selectedFactors.Select(x=>x["factor_key"]?.Type==JTokenType.String?
            new JObject{["factor_key"]=x["factor_key"],["min_stars"]=1}:new JObject{["raw_name"]=x["raw_name"],["min_stars"]=1}))};
    private void SetFactorText(string text){editingFactorText=true;factorSearch.Text=text;editingFactorText=false;UpdateFactorSuggestions();}
    private void UpdateFactorSuggestions()
    {
        var tokens=ParentLibraryUiLogic.Tokens(factorSearch.Text);
        factorSuggestions.SelectedItem=null;
        var rawScenarios=scenarioNames.Concat(observedRawNames).Distinct(StringComparer.Ordinal).Where(name=>!factorCatalog.Any(x=>Value(x,"name","")==name))
            .Select(name=>new JObject{["raw_name"]=name,["name"]=name,["kind"]="white",["category"]=scenarioNames.Contains(name)?"scenario":"white",["is_raw"]=true});
        var choices=tokens.Length==0?new List<JObject>():ParentLibraryUiLogic.Suggest(factorCatalog.Concat(rawScenarios),tokens[0]).ToList();
        if(tokens.Length>0&&!choices.Any(x=>(bool?)x["is_raw"]==true&&Value(x,"raw_name","")==tokens[0]))
            choices.Add(new JObject{["raw_name"]=tokens[0],["name"]=tokens[0],["is_raw"]=true});
        factorSuggestions.ItemsSource=choices;factorSuggestions.IsVisible=choices.Count>0;
        factorHint.Text=tokens.Length==0?(selectedFactors.Count==0?"输入部分或近似名称，从候选中选成词条；逗号可分隔多个因子。":"按"+(factorMode.SelectedIndex==1?"任意":"全部")+"词条筛选。"):
            "待确认："+tokens[0]+(tokens.Length>1?$" · 后面还有 {tokens.Length-1} 项":"")+"；选目录候选，或明确按原文匹配。";
    }
    private void AddFactorChip(JObject row)
    {
        var key=Value(row,"factor_key","");var raw=Value(row,"raw_name","").Trim();
        if(key.Length==0&&raw.Length==0)return;
        if(selectedFactors.Any(x=>key.Length>0?Value(x,"factor_key","")==key:Value(x,"factor_key","").Length==0&&Value(x,"raw_name","")==raw))return;
        selectedFactors.Add((JObject)row.DeepClone());RenderFactorChips();
    }
    private void RenderFactorChips()
    {
        factorChips.Children.Clear();
        foreach(var row in selectedFactors.ToArray()){
            var chip=new Button{Content=(Value(row,"factor_key","").Length==0?"原文 · ":"")+Value(row,"name","因子")+"  ×",Margin=new Thickness(0,0,6,3),Padding=new Thickness(9,4)};
            ToolTip.SetTip(chip,"移除此因子条件");chip.Click+=async(_,_)=>{selectedFactors.Remove(row);RenderFactorChips();UpdateFactorSuggestions();await Refresh();};factorChips.Children.Add(chip);
        }
    }
    public async Task ApplyFilters()
    {
        if(busy)return;await EnsureFactorCatalog();
        var pending=new List<string>();
        foreach(var token in ParentLibraryUiLogic.Tokens(factorSearch.Text)){
            var match=ParentLibraryUiLogic.Exact(factorCatalog,token);
            if(match==null)pending.Add(token);else AddFactorChip(match);
        }
        SetFactorText(string.Join("，",pending));
        if(pending.Count>0){status.Text="还有因子名称需要从目录候选确认，尚未应用这些词条。";return;}
        await Refresh();
    }
    private void ShowScan(JObject response)
    {
        var raw=response["scan_status"];
        if(raw is JObject job){
            scanAvailable=(bool?)job["available"]==true;scanRunning=(bool?)job["running"]==true;
            scanId=Value(job,"scan_id","");
            var endpoint=Value(job,"endpoint","");
            actualTarget.IsVisible=endpoint.Length>0;
            actualTarget.Text=(scanRunning?"本次扫描实际目标：":"最近扫描目标：")+endpoint;
            var progress=job["scanned"]?.Type==JTokenType.Integer?" · 已扫描 "+job["scanned"]+(job["total"]?.Type==JTokenType.Integer?" / "+job["total"]:"")+" 匹":"";
            scan.Text=Value(job,"message","扫描状态待确认")+progress;
            if((bool?)job["complete"]==true)scan.Text+=" · 已完成当前库";
            Lock(busy);return;
        }
        scanAvailable=scanRunning=false;scanId=null;
        actualTarget.IsVisible=false;
        scan.Text=raw is JObject value ? Value(value,"message","扫描状态待确认") : (string?)raw switch {
            "awaiting_calibration"=>"扫描页面待校准 · 当前可以导入、浏览与校对本地记录。",
            "ready"=>"扫描页面已校准 · 采集入口由面板提供。",
            "scanning"=>"正在采集种马库，已保存的记录仍可查看。",
            _=>"扫描状态待确认 · 当前可以查看本地记录。"};
        Lock(busy);
    }

    public async Task Refresh()
    {
        if(busy)return;if(explicitDeviceSelection&&!devicesLoaded)await RefreshDevices();Lock(true);var prior=Value(selected,"entry_id","");
        try {
            await EnsureFactorCatalog();var command=FilterRequest("parent_library_list");command["query"]=search.Text??"";
            var result=await request(command);
            ShowScan(result);revision=(int?)result["library"]?["revision"];
            var loadedRows=(result["entries"] as JArray??new()).OfType<JObject>().ToArray();
            foreach(var item in loadedRows)foreach(var rawName in (item["assessment"]?["summary"]?["pedigree"]?["known_raw_stars"] as JObject??new()).Properties())observedRawNames.Add(rawName.Name);
            UpdateFactorSuggestions();
            var rows=loadedRows.Where(x=>selectedFactors.Count==0||(string?)x["assessment"]?["status"]=="known_satisfied").ToArray();
            selecting=true;entries.ItemsSource=rows;entries.SelectedItem=rows.FirstOrDefault(r=>Value(r,"entry_id","")==prior)??rows.FirstOrDefault();selecting=false;
            count.Text=selectedFactors.Count==0?$"共 {rows.Length} 匹成品":$"{(factorMode.SelectedIndex==1?"任意":"全部")}匹配：{rows.Length} 匹";status.Text="记录保存在本机；导出不会上传。";
        }catch(Exception){status.Text="本地种马库未读成功，请重试。";}
        finally{selecting=false;Lock(false);}
        await LoadSelection();
    }
    private async Task LoadSelection()
    {
        var generation=++selectionGeneration;
        if(entries.SelectedItem is not JObject row){selected=null;ShowEmpty();return;}
        var id=Value(row,"entry_id","");if(id.Length==0){ShowEmpty();return;}
        try {
            var command=FilterRequest("parent_library_get");command["entry_id"]=id;var result=await request(command);
            if(generation!=selectionGeneration)return;
            selected=result["entry"] as JObject??throw new InvalidOperationException();RenderEntry(result);
        }catch(Exception){if(generation==selectionGeneration){selected=null;details.Children.Clear();details.Children.Add(Card(Text("这匹成品的详情暂未读取成功。")));}}
    }
    private void RenderEntry(JObject result)
    {
        ClearDetails();if(selected==null){ShowEmpty();return;}
        var header=new StackPanel{Spacing=4};header.Children.Add(Text(EntryTitle(selected),18));
        var parents=selected["parents"] as JArray;
        var nodes=new JObject?[]{selected["self"] as JObject,parents?.ElementAtOrDefault(0) as JObject,parents?.ElementAtOrDefault(1) as JObject};
        var states=nodes.Select(ParentLibraryUiLogic.NodeState).ToArray();
        var state=states.Contains("unread")?"unread":states.Contains("review")?"review":"complete";
        var readState=Text(state=="unread"?"因子未读全":state=="review"?"有因子待校对":"因子已读全",13);
        var readExplanation=state=="complete"?"已读取本体与两亲代的完整范围。":"合计只含已确认星数，未知项以问号显示，没有按0计算。";
        ToolTip.SetTip(readState,readExplanation);
        readState.Name="ParentLibraryCompleteness";readState.Foreground=Brush.Parse(state!="complete"?"#9B6613":"#397045");header.Children.Add(readState);
        var portraitRow=new Grid{ColumnDefinitions=new("*,*,*"),ColumnSpacing=8};
        var portraits=result["portraits"] as JObject;
        var titles=new[]{"本体","亲代 1","亲代 2"};var portraitKeys=new[]{"self","parent_1","parent_2"};
        for(var i=0;i<3;i++){
            var visual=PortraitCard(titles[i],i==0?selected:nodes[i],portraits?[portraitKeys[i]] as JObject);
            Grid.SetColumn(visual,i);portraitRow.Children.Add(visual);
        }
        header.Children.Add(portraitRow);
        var information=new StackPanel{Spacing=4};information.Children.Add(Text(readExplanation,12));
        information.Children.Add(Text("成品编号："+Value(selected,"entry_id","未记录"),12));
        information.Children.Add(Text(Value(selected,"nickname","")+"  "+Value(selected,"trained_on","育成时间未记录"),13));
        var assessment=result["assessment"] as JObject;
        if(assessment!=null){
            information.Children.Add(Text(Value(assessment,"label","可用程度以已读取的因子为准；未知项不作满足。"),13));
            if(assessment["self"] is JObject self)information.Children.Add(Text("仅看本体："+Value(self,"label","信息不足"),13));
            if(assessment["pedigree"] is JObject pedigree)information.Children.Add(Text("连同亲代："+Value(pedigree,"label","信息不足"),13));
        }
        var duplicates=result["duplicate_candidates"] as JArray;
        if(duplicates?.Count>0){
            header.Children.Add(Text($"有 {duplicates.Count} 条相似记录待核对，未自动合并。",12));
            information.Children.Add(Text("相似记录编号："+string.Join("、",duplicates.Select(x=>x is JObject o?Value(o,"entry_id","未记录"):x.ToString())),12));
        }
        var recordInformation=new Expander{Header="记录信息与评估",Content=information};
        details.Children.Add(Card(header));
        var overview=new StackPanel{Spacing=5};overview.Children.Add(Text("本体＋两亲代 · 因子合计",17));
        var overviewRows=ParentLibraryUiLogic.Overview(selected,assessment?["summary"] as JObject,factorCatalog,scenarioNames);
        var categoryCounts=string.Join(" · ",new[]{("blue","蓝"),("red","红"),("green","绿"),("scenario","剧本"),("white","白")}.Select(group=>group.Item2+" "+overviewRows.Count(x=>Value(x,"category","white")==group.Item1)));
        var factorCounts=Text(categoryCounts+" · 全部展开，可向下滚动查看",12);factorCounts.Name="ParentLibraryFactorCounts";overview.Children.Add(factorCounts);
        if(!overviewRows.Any(x=>Value(x,"category","white")=="white")){var emptyWhite=Text("白因子：0 条记录",12);ToolTip.SetTip(emptyWhite,"尚未读全时不能据此判断没有。");overview.Children.Add(emptyWhite);}
        var tiles=new WrapPanel{Orientation=Orientation.Horizontal,Name="ParentLibraryFactorOverview"};
        foreach(var row in overviewRows){
            var total=(int?)row["known_total_stars"];var own=(int?)row["self_stars"];
            var totalText=total.HasValue?total+"★":"?★";
            var ownText=own==0?"":"（本体"+(own.HasValue?own+"★":"?")+"）";
            var label=Text(Value(row,"name","未识别因子")+totalText+ownText,13);
            var category=Value(row,"category","white");
            var color=category switch{"blue"=>"#EAF4FF","red"=>"#FFF0F3","green"=>"#EEF8EA","scenario"=>"#FFF6D8",_=>"#F1F3F6"};
            var chip=new Border{MaxWidth=300,Margin=new Thickness(0,0,6,6),Padding=new Thickness(10,6),CornerRadius=new CornerRadius(8),Background=Brush.Parse(color),Child=label};
            ToolTip.SetTip(chip,((bool?)row["contains_unmatched_raw"]==true?"含待匹配原文：仅按同色同名合计，未改写原记录。\n":(bool?)row["unmatched"]==true?"未匹配目录，保留原文。\n":"")+
                (!total.HasValue?"合计星数待确认。":(bool?)row["complete"]==true?"已知范围内的合计星数。":"仅含已确认的星数，尚有信息待核对。")+
                (!own.HasValue?"\n本体星数未知，未按0计算。":""));
            tiles.Children.Add(chip);
        }
        if(tiles.Children.Count==0)overview.Children.Add(Text("还没有可汇总的因子记录。",13));else overview.Children.Add(tiles);
        if((assessment?["summary"]?["pedigree"]?["duplicate_nodes"] as JArray)?.Count>0)overview.Children.Add(Text("重复亲代引用只计一次星数。",12));
        details.Children.Add(Card(overview));details.Children.Add(recordInformation);
        details.Children.Add(NodeCard("本体因子","self",selected["self"] as JObject));
        details.Children.Add(NodeCard("亲代 1 因子","parent1",parents?.ElementAtOrDefault(0) as JObject));
        details.Children.Add(NodeCard("亲代 2 因子","parent2",parents?.ElementAtOrDefault(1) as JObject));
    }
    private Control PortraitCard(string title,JObject? identity,JObject? portrait)
    {
        var body=new Grid{ColumnDefinitions=new("48,*"),ColumnSpacing=6};
        Control picture=new TextBlock{Text="无头像",FontSize=10,TextWrapping=TextWrapping.Wrap,HorizontalAlignment=HorizontalAlignment.Center,VerticalAlignment=VerticalAlignment.Center};
        var encoded=Value(portrait,"png_base64","");
        if(encoded.Length>0)try{
            using var stream=new MemoryStream(Convert.FromBase64String(encoded));var bitmap=new Bitmap(stream);portraitBitmaps.Add(bitmap);
            picture=new Image{Source=bitmap,Width=48,Height=48,Stretch=Stretch.Uniform};
        }catch{picture=Text("图未读",10);}
        var portraitBox=new Border{Width=48,Height=48,Background=Brush.Parse("#F1F3F6"),CornerRadius=new CornerRadius(6),Child=picture};
        ToolTip.SetTip(portraitBox,encoded.Length==0?"未提供头像；不会根据名字猜图。":title+" · 保存的来源截图");body.Children.Add(portraitBox);
        var names=new StackPanel{Spacing=2,VerticalAlignment=VerticalAlignment.Center};names.Children.Add(Text(title,11));
        var identityText=Text(identity==null?"未读取":EntryTitle(identity),12);identityText.MaxLines=2;identityText.TextTrimming=TextTrimming.CharacterEllipsis;
        ToolTip.SetTip(identityText,identityText.Text);names.Children.Add(identityText);Grid.SetColumn(names,1);body.Children.Add(names);return body;
    }

    private Control NodeCard(string title,string nodeKey,JObject? node)
    {
        var panel=new StackPanel{Spacing=8};panel.Children.Add(Text(title,17));
        if(node==null){panel.Children.Add(Text("未读取 · 不能据此判断没有因子。",13));return Card(panel);}
        if(nodeKey!="self")panel.Children.Add(Text(EntryTitle(node),13));
        var nodeState=ParentLibraryUiLogic.NodeState(node);
        panel.Children.Add(Text(nodeState=="complete"?"已读完所选范围":nodeState=="review"?"有因子待校对":"因子未读全",13));
        if(node["scope"] is JArray scope)panel.Children.Add(Text("读取范围："+string.Join("、",scope.Values<string>().Select(Kind)),12));
        if(node["missing"] is JArray missing&&missing.Count>0)panel.Children.Add(Text("缺失信息："+string.Join("、",missing.Select(x=>x.ToString())),12));
        var factors=(node["factors"] as JArray??new()).OfType<JObject>().ToArray();
        if(factors.Length==0)panel.Children.Add(Text("暂无因子记录。",13));
        for(var index=0;index<factors.Length;index++){
            var factor=factors[index];var row=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=8};var lines=new StackPanel{Spacing=3};
            lines.Children.Add(Text(Kind((string?)factor["kind"])+" · "+Value(factor,"raw_name","名称未识别")+"  "+Stars(factor["stars"]),14));
            var key=Value(factor,"factor_key","");
            if(key.Length==0)lines.Children.Add(Text("名称尚未匹配目录，待校对",12));
            if(factor["sources"] is JArray sources&&sources.Count>0)lines.Children.Add(Text("来源页："+string.Join("、",sources.OfType<JObject>().Select(x=>x["page"]?.ToString()??"未知")),11));
            var edit=new Button{Content="校对",Name=$"ParentFactorEdit_{nodeKey}_{index}"};var captured=index;
            edit.Click+=async(_,_)=>await EditFactor(nodeKey,captured,(JObject)factor.DeepClone());
            row.Children.Add(lines);Grid.SetColumn(edit,1);row.Children.Add(edit);panel.Children.Add(row);
        }
        return Card(panel);
    }

    public async Task ImportDocument(JObject library)
    {
        if(busy||scanRunning||!revision.HasValue)return;Lock(true);
        try {var result=await request(new JObject{["action"]="parent_library_import",["library"]=library.DeepClone(),["expected_revision"]=revision});
            revision=(int?)result["library"]?["revision"]??revision;}
        catch(Exception error){status.Text=MutationFailure(error);return;}
        finally{Lock(false);}
        await Refresh();
        status.Text="共享库已替换为导入文件中的记录。";
    }
    public async Task<JObject> ExportDocument(bool planningSnapshot=false)
    {
        var response=await request(planningSnapshot?FilterRequest("parent_library_planner_snapshot"):new JObject{["action"]="parent_library_export"});
        return (JObject)(response[planningSnapshot?"snapshot":"library"] as JObject??throw new InvalidOperationException("导出内容未读完整")).DeepClone();
    }
    private async Task ImportFile()
    {
        if(busy||TopLevel.GetTopLevel(this) is not TopLevel owner)return;
        try {
            var files=await owner.StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions{Title="导入并替换共享种马库（建议先导出备份）",AllowMultiple=false,
                FileTypeFilter=new[]{new FilePickerFileType("种马库 JSON"){Patterns=new[]{"*.json"}}}});
            if(files.Count==0)return;
            await using var stream=await files[0].OpenReadAsync();
            if(stream.CanSeek&&stream.Length>16*1024*1024)throw new InvalidDataException();
            using var reader=new StreamReader(stream);await ImportDocument(JObject.Parse(await reader.ReadToEndAsync()));
        }catch(Exception){status.Text="文件未导入，请选择有效的种马库 JSON。";}
    }
    private async Task ExportFile(bool planningSnapshot)
    {
        if(busy||TopLevel.GetTopLevel(this) is not TopLevel owner)return;
        try {
            var file=await owner.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions{Title=planningSnapshot?"导出规划快照":"导出本地种马库",
                SuggestedFileName=planningSnapshot?"MaaUma-种马规划快照.json":"MaaUma-种马库.json",DefaultExtension="json",
                FileTypeChoices=new[]{new FilePickerFileType("JSON"){Patterns=new[]{"*.json"}}}});
            if(file==null)return;Lock(true);var document=await ExportDocument(planningSnapshot);
            await using var stream=await file.OpenWriteAsync();stream.SetLength(0);await using var writer=new StreamWriter(stream);
            await writer.WriteAsync(document.ToString(Formatting.Indented));
            status.Text=planningSnapshot?"规划快照已导出。":"本地种马库已导出。";
        }catch(Exception){status.Text="导出未完成，请重试。";}finally{Lock(false);}
    }

    private static string MutationFailure(Exception error)=>error.Message.Contains("revision",StringComparison.OrdinalIgnoreCase)||error.Message.Contains("冲突")||error.Message.Contains("已更新")||error.Message.Contains("另一面板")||error.Message.Contains("正在保存")
        ?"另一面板已更新种马库，本次修改未覆盖。请刷新后重新校对或导入。":"保存未完成，请核对内容后重试；原记录未被替换。";

    public async Task<bool> CorrectFactor(string entryId,string node,int index,JObject changes,int expectedRevision,string note="")
    {
        if(busy||scanRunning)return false;Lock(true);
        try {
            var result=await request(new JObject{["action"]="parent_library_correct",["entry_id"]=entryId,["node"]=node,
                ["factor_index"]=index,["changes"]=changes.DeepClone(),["note"]=note,["expected_revision"]=expectedRevision});
            revision=(int?)result["library"]?["revision"]??revision;
            status.Text="校对已保存，原始来源保留；单项修正不会把未读完整的页面标成完整。";
            return true;
        }catch(Exception error){status.Text=MutationFailure(error);return false;}
        finally{Lock(false);}
    }

    private async Task EditFactor(string node,int index,JObject factor)
    {
        if(busy||scanRunning||selected==null||!revision.HasValue||TopLevel.GetTopLevel(this) is not Window owner)return;
        var entryId=Value(selected,"entry_id","");var expectedRevision=revision.Value;
        JObject[] catalog=[];
        try{var result=await request(new JObject{["action"]="parent_library_catalog"});catalog=(result["factors"] as JArray??new()).OfType<JObject>().ToArray();}catch{/* Manual raw-name/star corrections remain available. */}
        if(Value(selected,"entry_id","")!=entryId)return;
        var window=new Window{Title="校对因子 · "+EntryTitle(selected),Width=530,Height=650,MinWidth=400,MinHeight=450,WindowStartupLocation=WindowStartupLocation.CenterOwner};
        var kind=new ComboBox{ItemsSource=new[]{"蓝因子","红因子","白因子","绿因子"},SelectedIndex=Array.IndexOf(new[]{"blue","red","white","green"},(string?)factor["kind"])};
        var name=new TextBox{Name="ParentFactorRawName",Text=Value(factor,"raw_name",""),Watermark="填写游戏中实际显示的名称"};
        var stars=new ComboBox{Name="ParentFactorStars",ItemsSource=new[]{"未识别","1 星","2 星","3 星"},SelectedIndex=(int?)factor["stars"]??0};
        var catalogSearch=new TextBox{Watermark="搜索目录中的因子名称"};
        var matched=new ComboBox{Name="ParentFactorCatalog",HorizontalAlignment=HorizontalAlignment.Stretch};
        var originalKey=Value(factor,"factor_key","");
        void FilterCatalog(string? keep=null){
            var selectedKey=keep??(matched.SelectedItem as CatalogChoice)?.Key;
            var selectedKind=kind.SelectedIndex is >=0 and <=3?new[]{"blue","red","white","green"}[kind.SelectedIndex]:"";
            var query=catalogSearch.Text??"";
            var choices=catalog.Where(x=>(string?)x["kind"]==selectedKind&&Value(x,"name","").Contains(query,StringComparison.OrdinalIgnoreCase))
                .Select(x=>new CatalogChoice((string?)x["factor_key"],selectedKind,Value(x,"name","未命名目录项"))).ToList();
            if(!string.IsNullOrEmpty(selectedKey)&&selectedKey.StartsWith(selectedKind+":",StringComparison.Ordinal)&&choices.All(x=>x.Key!=selectedKey))
                choices.Insert(0,new CatalogChoice(selectedKey,selectedKind,"保留当前匹配："+selectedKey));
            choices.Insert(0,new CatalogChoice(null,"","尚未匹配目录（保持未知）"));
            matched.ItemsSource=choices;matched.SelectedItem=choices.FirstOrDefault(x=>x.Key==selectedKey)??choices[0];
        }
        FilterCatalog(originalKey);catalogSearch.TextChanged+=(_,_)=>FilterCatalog();kind.SelectionChanged+=(_,_)=>FilterCatalog();
        var note=new TextBox{Watermark="校对说明（可选）"};
        var body=new StackPanel{Margin=new Thickness(22),Spacing=10};
        body.Children.Add(Text("只修正这匹成品的这一条因子，来源及原读数会保留。",13));
        foreach(var pair in new (string,Control)[]{("颜色",kind),("游戏中显示的名称",name),("星级",stars),("校对说明",note)}){
            body.Children.Add(Text(pair.Item1,12));body.Children.Add(pair.Item2);
        }
        body.Children.Add(Text("匹配因子目录（不确定时保持未知）",12));body.Children.Add(catalogSearch);body.Children.Add(matched);
        if(catalog.Length==0)body.Children.Add(Text("当前没有可用目录，仍可修正名称与星级；原有匹配保留，未知项不会自动猜测。",12));
        var message=Text("",12);body.Children.Add(message);
        var buttons=new StackPanel{Orientation=Orientation.Horizontal,Spacing=8};var save=new Button{Name="ParentFactorSave",Content="保存校对"};var cancel=new Button{Name="ParentFactorCancel",Content="取消"};
        buttons.Children.Add(save);buttons.Children.Add(cancel);body.Children.Add(buttons);window.Content=new ScrollViewer{Content=body};
        cancel.Click+=(_,_)=>window.Close(false);
        save.Click+=async(_,_)=>{
            if(kind.SelectedIndex is <0 or >3||stars.SelectedIndex is <0 or >3||string.IsNullOrWhiteSpace(name.Text)){message.Text="请填写实际名称并选择颜色；星级不清楚可保持未识别。";return;}
            save.IsEnabled=false;
            var changes=new JObject{["kind"]=new[]{"blue","red","white","green"}[kind.SelectedIndex],["raw_name"]=name.Text!.Trim(),
                ["stars"]=stars.SelectedIndex==0?null:stars.SelectedIndex,["factor_key"]=(matched.SelectedItem as CatalogChoice)?.Key};
            if(await CorrectFactor(entryId,node,index,changes,expectedRevision,note.Text??""))window.Close(true);
            else{message.Text=status.Text;save.IsEnabled=true;}
        };
        if(await window.ShowDialog<bool>(owner))await LoadSelection();
    }

    private async Task ConfirmScan()
    {
        if(!scanAvailable||scanRunning||busy||TopLevel.GetTopLevel(this) is not Window owner)return;
        var confirmed=devices.SelectedItem as JObject;
        if(explicitDeviceSelection&&confirmed==null){status.Text="请先选择本次扫描设备。";return;}
        var confirmedId=explicitDeviceSelection?Value(confirmed,"id",""):null;
        var dialog=new Window{Title="开始只读扫描",Width=510,SizeToContent=SizeToContent.Height,CanResize=false,WindowStartupLocation=WindowStartupLocation.CenterOwner};
        var body=new StackPanel{Margin=new Thickness(22),Spacing=14};
        if(explicitDeviceSelection)body.Children.Add(Text("本次固定扫描："+DeviceLabel(confirmed)+"\nADB："+Value(confirmed,"adb_path","未配置"),14));
        body.Children.Add(Text(explicitDeviceSelection?"请先把以上设备停在名人堂列表，关闭筛选，并回到列表顶部。":"请先把当前选中的设备停在名人堂列表，关闭筛选，并回到列表顶部。",16));
        body.Children.Add(Text("本次只打开详情、翻页和返回读取因子；不会切换账号、消费资源或修改育成。停止时保留已经扫描的成品。",13));
        var buttons=new StackPanel{Orientation=Orientation.Horizontal,Spacing=8};var yes=new Button{Content="已准备好，开始扫描"};var cancel=new Button{Content="取消"};
        yes.Click+=(_,_)=>dialog.Close(true);cancel.Click+=(_,_)=>dialog.Close(false);buttons.Children.Add(yes);buttons.Children.Add(cancel);body.Children.Add(buttons);dialog.Content=body;
        if(await dialog.ShowDialog<bool>(owner))await BeginScan(confirmedId);
    }
    public async Task BeginScan(string? confirmedDeviceId=null)
    {
        if(busy||scanPolling||loadingDevices||scanRunning||!scanAvailable)return;
        var command=new JObject{["action"]="parent_library_scan_start"};
        if(explicitDeviceSelection){
            var id=confirmedDeviceId??Value(devices.SelectedItem as JObject,"id","");
            if(string.IsNullOrEmpty(id)){status.Text="请先选择本次扫描设备。";return;}
            command["device_id"]=id;
        }
        if(!string.IsNullOrWhiteSpace(scanLimit.Text)){
            if(!int.TryParse(scanLimit.Text,out var limit)||limit is <1 or >2000){status.Text="扫描上限请填写 1～2000，或留空扫描全库。";return;}
            command["limit"]=limit;
        }
        Lock(true);
        try{ShowScan(await request(command));status.Text="已请求只读扫描，进度以上方后台状态为准。";}
        catch(InvalidOperationException error){status.Text="扫描未开始："+error.Message;}
        catch(Exception){status.Text="扫描尚未开始，请核对选中的设备和名人堂列表后重试。";}
        finally{Lock(false);}
    }
    public async Task RefreshScan()
    {
        if(scanPolling||busy)return;scanPolling=true;Lock(busy);var wasRunning=scanRunning;
        try{ShowScan(await request(new JObject{["action"]="parent_library_scan_status",["scan_id"]=scanId}));}
        catch(Exception){scan.Text="扫描进度暂未读到，可点击刷新重试；未自动重发开始或停止。";}
        finally{scanPolling=false;Lock(busy);}
        if(wasRunning&&!scanRunning)await Refresh();
    }
    public async Task StopScan()
    {
        if(busy||scanPolling||!scanRunning||string.IsNullOrEmpty(scanId))return;Lock(true);
        try{ShowScan(await request(new JObject{["action"]="parent_library_scan_stop",["scan_id"]=scanId}));status.Text="已请求停止扫描，已读取的记录会保留。";}
        catch(Exception){status.Text="停止请求未确认，请刷新进度核对；不会停止其他扫描。";}
        finally{Lock(false);}
        if(!scanRunning)await Refresh();
    }
}

// Presentation-only helpers. Canonical totals come from the backend summary,
// which already deduplicates scroll observations and repeated parent references.
internal static class ParentLibraryUiLogic
{
    internal static string[] Tokens(string? text)=>(text??"").Split(new[]{',','，','\n','\r'},StringSplitOptions.RemoveEmptyEntries).Select(x=>x.Trim()).Where(x=>x.Length>0).ToArray();
    private static string Normalize(string value)=>new(value.Normalize(NormalizationForm.FormKC).Where(c=>!char.IsWhiteSpace(c)&&!char.IsPunctuation(c)).Select(char.ToLowerInvariant).ToArray());
    private static IEnumerable<string> Names(JObject row)=>new[]{(string?)row["name"]??""}.Concat((row["aliases"] as JArray??new()).Values<string>().OfType<string>());
    internal static JObject? Exact(IEnumerable<JObject> catalog,string token)
    {
        var normalized=Normalize(token);var rows=catalog.Where(x=>Names(x).Any(n=>Normalize(n)==normalized)).GroupBy(x=>(string?)x["factor_key"]).Select(x=>x.First()).ToArray();
        return normalized.Length>0&&rows.Length==1?rows[0]:null;
    }
    internal static JObject[] Suggest(IEnumerable<JObject> catalog,string token)
    {
        var input=Normalize(token);if(input.Length==0)return Array.Empty<JObject>();
        int Score(string name){var value=Normalize(name);if(value==input)return 0;if(value.Contains(input)||input.Contains(value)&&value.Length>0)return 1;
            var distance=Distance(value,input);return distance<=(Math.Min(value.Length,input.Length)<=3?1:2)?10+distance:100;}
        return catalog.Select(x=>(row:x,score:Names(x).Min(Score))).Where(x=>x.score<100)
            .OrderBy(x=>x.score).ThenBy(x=>(string?)x.row["name"],StringComparer.Ordinal).Take(12).Select(x=>x.row).ToArray();
    }
    private static int Distance(string a,string b)
    {
        var prior=Enumerable.Range(0,b.Length+1).ToArray();
        for(var i=1;i<=a.Length;i++){var row=new int[b.Length+1];row[0]=i;for(var j=1;j<=b.Length;j++)row[j]=Math.Min(Math.Min(row[j-1]+1,prior[j]+1),prior[j-1]+(a[i-1]==b[j-1]?0:1));prior=row;}
        return prior[b.Length];
    }
    private static string Text(JObject? row,string key,string fallback="")=>row?[key]?.Type==JTokenType.String?(string?)row[key]??fallback:fallback;
    private static int? Number(JObject? map,string key)=>map?[key]?.Type==JTokenType.Integer?map[key]!.Value<int>():null;
    private static JObject[] Rows(JObject? node)=>(node?["factors"] as JArray??new()).OfType<JObject>().ToArray();
    private static bool Complete(JObject? summary,string kind)=>(summary?["complete_kinds"] as JArray??new()).Values<string>().Contains(kind);
    internal static string NodeState(JObject? node)
    {
        if(node==null)return "unread";
        var scope=(node["scope"] as JArray??new()).Values<string>().ToArray();
        if(!new[]{"blue","red","white","green"}.All(scope.Contains))return "unread";
        var missing=(node["missing"] as JArray??new()).Values<string>().Where(x=>!string.IsNullOrWhiteSpace(x)).ToArray();
        var readingIssues=new[]{"unresolved_factor_name","unknown_factor_stars","duplicate_factor_rows"};
        if(missing.Any(x=>!readingIssues.Any(prefix=>x!.StartsWith(prefix,StringComparison.Ordinal))))return "unread";
        if(missing.Length>0||Rows(node).Any(x=>x["factor_key"]?.Type!=JTokenType.String||x["stars"]?.Type!=JTokenType.Integer))return "review";
        return Text(node,"status")=="complete"?"complete":"unread";
    }
    internal static JObject[] Overview(JObject entry,JObject? summary,JObject[] catalog,ISet<string> scenarioNames)
    {
        var parents=entry["parents"] as JArray;
        var nodes=new Dictionary<string,JObject?>{["self"]=entry["self"] as JObject,["parent1"]=parents?.ElementAtOrDefault(0) as JObject,["parent2"]=parents?.ElementAtOrDefault(1) as JObject};
        var all=nodes.Values.SelectMany(Rows).ToArray();var own=Rows(nodes["self"]);
        var pedigree=summary?["pedigree"] as JObject;var self=summary?["self"] as JObject;
        var known=pedigree?["known_stars"] as JObject;var ownKnown=self?["known_stars"] as JObject;
        var rawKnown=pedigree?["known_raw_stars"] as JObject;var ownRawKnown=self?["known_raw_stars"] as JObject;
        var result=new List<JObject>();var mergedRaw=new HashSet<(string kind,string name)>();
        foreach(var key in all.Select(x=>Text(x,"factor_key")).Concat(known?.Properties().Select(x=>x.Name)??Enumerable.Empty<string>()).Where(x=>x.Length>0).Distinct()){
            var item=catalog.FirstOrDefault(x=>Text(x,"factor_key")==key);var observed=all.FirstOrDefault(x=>Text(x,"factor_key")==key);
            var kind=key.Split(':')[0];var name=Text(item,"name",Text(observed,"raw_name",key));
            var sameKey=all.Where(x=>Text(x,"factor_key")==key).ToArray();
            // A canonical name and an unkeyed observation may share one display chip
            // only under an exact same-colour/name proof. Stored keys stay untouched.
            var mergeRaw=item!=null&&Text(item,"kind")==kind&&sameKey.Length>0&&sameKey.All(x=>Text(x,"raw_name")==name)&&
                all.Any(x=>Text(x,"factor_key").Length==0&&Text(x,"kind")==kind&&Text(x,"raw_name")==name);
            var total=mergeRaw?Number(rawKnown,name):Number(known,key);var ownStars=mergeRaw?Number(ownRawKnown,name):Number(ownKnown,key);
            if(mergeRaw){
                mergedRaw.Add((kind,name));
                if(!ownStars.HasValue&&(bool?)self?["raw_names_complete"]==true&&!own.Any(x=>Text(x,"raw_name")==name))ownStars=0;
            }else if(!ownStars.HasValue&&Complete(self,kind)&&!own.Any(x=>Text(x,"factor_key")==key||Text(x,"factor_key").Length==0&&Text(x,"kind")==kind))ownStars=0;
            var category=(bool?)item?["is_scenario"]==true||Text(item,"category")=="scenario"?"scenario":kind;
            result.Add(new JObject{["factor_key"]=key,["name"]=name,["kind"]=kind,["category"]=category,["known_total_stars"]=total,
                ["self_stars"]=ownStars,["complete"]=total.HasValue&&(mergeRaw?(bool?)pedigree?["raw_names_complete"]==true:Complete(pedigree,kind)),
                ["unmatched"]=false,["contains_unmatched_raw"]=mergeRaw});
        }
        foreach(var group in all.Where(x=>Text(x,"factor_key").Length==0).GroupBy(x=>(kind:Text(x,"kind"),name:Text(x,"raw_name","名称未识别")))){
            if(mergedRaw.Contains(group.Key))continue;
            var total=Number(rawKnown,group.Key.name);var ownStars=Number(ownRawKnown,group.Key.name);
            var selfComplete=(bool?)self?["raw_names_complete"]==true;
            var allComplete=(bool?)pedigree?["raw_names_complete"]==true;
            if(!ownStars.HasValue&&selfComplete&&!own.Any(x=>Text(x,"raw_name")==group.Key.name))ownStars=0;
            result.Add(new JObject{["factor_key"]=null,["name"]=group.Key.name,["kind"]=group.Key.kind,
                ["category"]=scenarioNames.Contains(group.Key.name)?"scenario":group.Key.kind,
                ["known_total_stars"]=total,["self_stars"]=ownStars,["complete"]=allComplete&&total.HasValue,["unmatched"]=true});
        }
        int Order(JObject x)=>Text(x,"category") switch{"blue"=>0,"red"=>1,"green"=>2,"scenario"=>3,_=>4};
        return result.OrderBy(Order).ThenBy(x=>Text(x,"name"),StringComparer.Ordinal).ToArray();
    }
}
