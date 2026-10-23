using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Manual account inventory with its own revision, independent from finished horses.</summary>
public sealed class TeamPlanningInventoryEditor : StackPanel
{
    private sealed record Choice(string Id,string Label,JObject? Row=null){public override string ToString()=>Label;}
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<string?> account;
    private readonly Dictionary<string,Group> groups=new();
    private readonly TextBlock status=TeamPlanningUi.Note("选定账号后可记录持有资料；不需要先扫描完整账号。" );
    private bool busy,loading;private int generation;private string? loadedAccount;private readonly Dictionary<string,int> searches=new();
    private readonly Dictionary<string,JObject> unsavedByAccount=new(StringComparer.Ordinal);
    private string displayedAccount="";
    private void RememberInventoryEdits()
    {
        if(!HasUnsavedChanges){unsavedByAccount.Remove(displayedAccount);return;}
        var draft=new JObject{["revision"]=Revision,["ready"]=loadedAccount==displayedAccount};
        foreach(var group in groups)draft[group.Key]=group.Value.CaptureDraft();
        unsavedByAccount[displayedAccount]=draft;
    }
    private void RestoreInventoryEdits(JObject draft,bool keepRevision=false)
    {
        if(!keepRevision)Revision=TeamPlanningUi.Int(draft["revision"])??0;
        loading=true;try{foreach(var group in groups)if(draft[group.Key] is JObject value)group.Value.RestoreDraft(value);}finally{loading=false;}
    }
    public bool IsReady=>account()==null||loadedAccount==account();
    public int Revision{get;private set;}
    public bool HasUnsavedChanges=>groups.Values.Any(x=>x.Dirty);
    public event Action? Changed;
    public TeamPlanningInventoryEditor(Func<JObject,Task<JObject>> request,Func<string?> account)
    {
        this.request=request;this.account=account;Name="TeamInventoryEditor";Spacing=8;
        Children.Add(TeamPlanningUi.Note("资料未知可以留空；未录入的卡不等于没有。角色与支援卡分别保存，使用独立库存版本检查。"));Children.Add(status);
        foreach(var info in new[]{("characters","角色"),("support_cards","支援卡")}){var group=new Group(this,info.Item1,info.Item2);groups[info.Item1]=group;Children.Add(group);}
    }
    private void Edited(){if(loading)return;generation++;Changed?.Invoke();status.Text="持有资料有未保存的修改；保存后，AI 才能使用这些资料。";}
    public async Task Load()
    {
        RememberInventoryEdits();var id=account();var key=id??"";var same=key==displayedAccount;
        var oldLoaded=loadedAccount;var oldRevision=Revision;var at=++generation;
        unsavedByAccount.TryGetValue(key,out var draft);
        if(!same){loading=true;try{foreach(var group in groups.Values)group.Load(new JObject());}finally{loading=false;}}
        displayedAccount=key;loadedAccount=null;
        if(id==null){Revision=0;if(draft!=null)RestoreInventoryEdits(draft);status.Text="当前为参考方案，持有状态未知。选定账号后可保存持有资料；原账号的未保存编辑仍保留。";return;}
        if(draft!=null&&(bool?)draft["ready"]==true){RestoreInventoryEdits(draft);loadedAccount=id;status.Text="已恢复此账号未保存的编辑；保存后用于生成方案。刷新不会清空这些内容。";return;}
        try{
            var response=await request(new JObject{["action"]="parent_planning_team_inventory_get",["account_id"]=id});
            if(at!=generation||id!=account())return;Apply(response);loadedAccount=id;
            if(draft!=null){RestoreInventoryEdits(draft,keepRevision:true);RememberInventoryEdits();}
            status.Text=draft!=null?"已读到账号资料版本，并恢复未保存的编辑。":"已读取账号持有资料；未知等级、突破与未录入项目保持未知。";
        }catch(InvalidOperationException e){if(at==generation){if(draft!=null)RestoreInventoryEdits(draft);if(same){loadedAccount=oldLoaded;if(draft==null)Revision=oldRevision;}status.Text="资料刷新未完成，编辑已保留："+e.Message;}}
        catch{if(at==generation){if(draft!=null)RestoreInventoryEdits(draft);if(same){loadedAccount=oldLoaded;if(draft==null)Revision=oldRevision;}status.Text="账号持有资料暂未读取；未保存的内容保留，可以重试。";}}
    }
    private void Apply(JObject response)
    {
        Revision=TeamPlanningUi.Int(response["inventory_revision"])??0;loading=true;try{foreach(var group in groups)group.Value.Load(response[group.Key] as JObject??new());}finally{loading=false;}
    }
    public async Task Search(string kind,string query)
    {
        var at=generation;var searchAt=searches.TryGetValue(kind,out var prior)?prior+1:1;searches[kind]=searchAt;try{var response=await request(new JObject{["action"]="parent_planning_team_inventory_catalog",["kind"]=kind,["query"]=query,["limit"]=40});if(at!=generation||searches[kind]!=searchAt)return;groups[kind].SearchResult((response[kind] as JArray??new()).OfType<JObject>().ToArray());}catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="本地名称搜索暂不可用；可先记录原文与未知状态。";}
    }
    public async Task Save(string kind)
    {
        if(busy)return;if(!IsReady){status.Text="请先等待当前账号资料读取完成，或刷新后重试。";return;}var id=account();if(id==null){status.Text="请先选定账号，再保存持有资料。";return;}if(!groups[kind].TryRead(out var value,out var message)){status.Text=message;return;}var at=generation;busy=true;groups[kind].SaveButton.IsEnabled=false;
        try{var response=await request(new JObject{["action"]="parent_planning_team_inventory_save",["account_id"]=id,["expected_revision"]=Revision,[kind]=value});if(account()!=id||at!=generation){status.Text="点击时的持有资料已保存；当前编辑有变化，请刷新或继续核对。";return;}Revision=TeamPlanningUi.Int(response["inventory_revision"])??Revision;loading=true;try{groups[kind].Load(response[kind] as JObject??value);}finally{loading=false;}generation++;RememberInventoryEdits();Changed?.Invoke();status.Text="此类持有资料已保存，其他类别的未保存编辑保留。";}
        catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="库存保存未完成，编辑保留；若其他窗口已保存，请刷新后重试。";}finally{busy=false;groups[kind].SaveButton.IsEnabled=true;}
    }
    private sealed class Group : StackPanel
    {
        private readonly TeamPlanningInventoryEditor owner;private readonly string kind;
        private readonly TextBox search=new(){Watermark="搜索本地目录名称"};
        private readonly ComboBox matches=new(){HorizontalAlignment=HorizontalAlignment.Stretch,PlaceholderText="选择目录条目"},coverage=new(){ItemsSource=new[]{"未知","部分记录","完整清单"},SelectedIndex=0};
        private readonly StackPanel rows=new(){Spacing=6};
        private readonly List<JObject> data=new();
        private string coverageState="unknown";
        private string CoverageValue=>coverage.SelectedIndex switch{1=>"partial",2=>"complete",_=>"unknown"};
        private readonly List<(JObject Row,TextBox Level,TextBox Break)> numeric=new();
        public Button SaveButton{get;}=new();
        public bool Dirty{get;private set;}
        public Group(TeamPlanningInventoryEditor owner,string kind,string label)
        {
            this.owner=owner;this.kind=kind;Name="TeamInventory_"+kind;Spacing=7;Children.Add(TeamPlanningUi.Title(label,15));
            search.Name="TeamInventorySearch_"+kind;matches.Name="TeamInventoryMatches_"+kind;SaveButton.Name="TeamInventorySave_"+kind;SaveButton.Content="保存"+label+"资料";
            var bar=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=6};bar.Children.Add(search);var find=new Button{Name="TeamInventoryFind_"+kind,Content="搜索"};find.Click+=async(_,_)=>await owner.Search(kind,(search.Text??"").Trim());Grid.SetColumn(find,1);bar.Children.Add(find);Children.Add(bar);Children.Add(matches);
            var add=new Button{Content="加入所选"+label,Name="TeamInventoryAdd_"+kind};add.Click+=(_,_)=>{if(matches.SelectedItem is Choice choice&&choice.Row!=null)Add(choice.Row);};Children.Add(add);
            var raw=new Button{Content="添加未收录名称",Name="TeamInventoryRaw_"+kind};raw.Click+=(_,_)=>{var name=(search.Text??"").Trim();if(name.Length==0)return;Add(new JObject{["name"]=name});};Children.Add(raw);
            coverage.Name="TeamInventoryCoverage_"+kind;coverage.SelectionChanged+=(_,_)=>{var value=CoverageValue;if(value==coverageState)return;coverageState=value;Edit();};Children.Add(TeamPlanningUi.Field("这份清单的范围",coverage));Children.Add(rows);Children.Add(SaveButton);SaveButton.Click+=async(_,_)=>await owner.Save(kind);
        }
        private void Edit(){if(owner.loading)return;Dirty=true;owner.Edited();}
        public void SearchResult(JObject[] values){matches.ItemsSource=values.Select(x=>new Choice(TeamPlanningUi.Str(x[kind=="characters"?"character_id":"card_id"]),TeamPlanningUi.Str(x["name"]),x)).ToArray();matches.SelectedIndex=values.Length>0?0:-1;}
        public void Load(JObject value){data.Clear();data.AddRange((value["items"] as JArray??new()).OfType<JObject>().Select(x=>(JObject)x.DeepClone()));coverageState=TeamPlanningUi.Str(value["coverage"]) is "partial" or "complete"?TeamPlanningUi.Str(value["coverage"]):"unknown";coverage.SelectedIndex=coverageState switch{"partial"=>1,"complete"=>2,_=>0};Dirty=false;Render();}
        private void Add(JObject catalog)
        {
            CaptureNumbers();
            var key=kind=="characters"?"character_id":"card_id";var id=catalog[key];if(id!=null&&id.Type!=JTokenType.Null&&data.Any(x=>JToken.DeepEquals(x[key],id)))return;
            var row=new JObject{[key]=id?.DeepClone(),["raw_name"]=TeamPlanningUi.Str(catalog["name"]),["status"]="unknown",["source"]="manual"};if(kind=="characters")row["variant_id"]=null;else{row["level"]=null;row["limit_break"]=null;}data.Add(row);if(coverage.SelectedIndex==0)coverage.SelectedIndex=1;Render();Edit();
        }
        private void Render()
        {
            rows.Children.Clear();numeric.Clear();foreach(var row in data.ToArray()){
                var body=new StackPanel{Spacing=5};var title=TeamPlanningUi.Str(row["raw_name"]);if(title.Length==0)title=kind=="characters"?"角色 #"+TeamPlanningUi.Str(row["character_id"]):"支援卡 #"+TeamPlanningUi.Str(row["card_id"]);body.Children.Add(TeamPlanningUi.Title(title,13));
                var statuses=kind=="characters"?new[]{("unknown","未知"),("owned","持有"),("not_owned","未持有")}:new[]{("unknown","未知"),("owned","持有"),("borrowable","可借"),("not_owned","未持有")};var state=new ComboBox{Name="TeamInventoryStatus_"+kind,ItemsSource=statuses.Select(x=>new Choice(x.Item1,x.Item2)).ToArray()};state.SelectedItem=((Choice[])state.ItemsSource!).FirstOrDefault(x=>x.Id==TeamPlanningUi.Str(row["status"]))??((Choice[])state.ItemsSource!)[0];state.SelectionChanged+=(_,_)=>{var value=(state.SelectedItem as Choice)?.Id??"unknown";if(value==TeamPlanningUi.Str(row["status"]))return;row["status"]=value;Edit();};body.Children.Add(state);
                if(kind=="characters"){
                    var variant=new TextBox{Name="TeamInventoryVariant",Watermark="衣装 ID 可留空（未知）",Text=TeamPlanningUi.Str(row["variant_id"])};variant.TextChanged+=(_,_)=>{var text=(variant.Text??"").Trim();if(text==TeamPlanningUi.Str(row["variant_id"]))return;row["variant_id"]=text.Length>0?new JValue(text):JValue.CreateNull();Edit();};body.Children.Add(variant);
                }else{
                    var level=new TextBox{Name="TeamInventoryLevel",Watermark="等级未知可留空",Text=TeamPlanningUi.Str(row["level"]),Width=150};var breaks=new TextBox{Name="TeamInventoryLimitBreak",Watermark="突破 0～4 / 未知",Text=TeamPlanningUi.Str(row["limit_break"]),Width=150};level.Tag=level.Text??"";breaks.Tag=breaks.Text??"";level.TextChanged+=(_,_)=>{var text=level.Text??"";if(text==(level.Tag as string??""))return;level.Tag=text;Edit();};breaks.TextChanged+=(_,_)=>{var text=breaks.Text??"";if(text==(breaks.Tag as string??""))return;breaks.Tag=text;Edit();};numeric.Add((row,level,breaks));var line=new WrapPanel();line.Children.Add(TeamPlanningUi.Field("等级",level));line.Children.Add(TeamPlanningUi.Field("突破",breaks));body.Children.Add(line);
                }
                var remove=new Button{Content="移除此条记录"};remove.Click+=(_,_)=>{CaptureNumbers();data.Remove(row);Render();Edit();};body.Children.Add(remove);rows.Children.Add(TeamPlanningUi.Card(body));
            }
            if(data.Count==0)rows.Children.Add(TeamPlanningUi.Note("尚未记录；这表示未知。"));
        }
        private void CaptureNumbers(){foreach(var item in numeric){item.Row["level"]=int.TryParse(item.Level.Text,out var level)?new JValue(level):JValue.CreateNull();item.Row["limit_break"]=int.TryParse(item.Break.Text,out var breaks)?new JValue(breaks):JValue.CreateNull();}}
        public JObject CaptureDraft()
        {
            return new JObject{["coverage"]=CoverageValue,["items"]=new JArray(data.Select(x=>x.DeepClone())),["dirty"]=Dirty,
                ["search"]=search.Text??"",["numeric_text"]=new JArray(numeric.Select(x=>new JObject{
                    ["index"]=data.FindIndex(row=>ReferenceEquals(row,x.Row)),["level"]=x.Level.Text??"",["limit_break"]=x.Break.Text??""}))};
        }
        public void RestoreDraft(JObject draft)
        {
            Load(draft);search.Text=TeamPlanningUi.Str(draft["search"]);
            foreach(var text in (draft["numeric_text"] as JArray??new()).OfType<JObject>()){
                var index=TeamPlanningUi.Int(text["index"]);if(index is not int i||i<0||i>=data.Count)continue;
                var item=numeric.FirstOrDefault(x=>ReferenceEquals(x.Row,data[i]));if(item.Level!=null){item.Level.Tag=TeamPlanningUi.Str(text["level"]);item.Break.Tag=TeamPlanningUi.Str(text["limit_break"]);item.Level.Text=(string)item.Level.Tag;item.Break.Text=(string)item.Break.Tag;}
            }
            Dirty=(bool?)draft["dirty"]==true;
        }
        public bool TryRead(out JObject value,out string message)
        {
            value=new();message="";foreach(var item in numeric){if(!Valid(item.Level,1,50)||!Valid(item.Break,0,4)){message="支援卡等级请填 1～50、突破填 0～4；未知时留空。";return false;}}CaptureNumbers();value=new JObject{["coverage"]=coverage.SelectedIndex switch{1=>"partial",2=>"complete",_=>"unknown"},["items"]=new JArray(data.Select(x=>x.DeepClone()))};return true;
        }
        private static bool Valid(TextBox box,int min,int max)=>string.IsNullOrWhiteSpace(box.Text)||(int.TryParse(box.Text,out var n)&&n>=min&&n<=max);
    }
}
