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
using Avalonia.Threading;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Local draft and read-only backend assessment; ownership is never inferred by this view.</summary>
public sealed class ParentLocalRouteView : StackPanel
{
    private static readonly string[] Keys=["parent_a","gp_a1","gp_a2","parent_b","gp_b1","gp_b2"];
    private static readonly string[] Titles=["亲代 A","祖辈 A1","祖辈 A2","亲代 B","祖辈 B1","祖辈 B2"];
    private sealed record Choice(string Id,string Label,JObject? Row=null){public override string ToString()=>Label;}
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<JObject> context;
    private readonly Func<string?[]> familyParents;
    private readonly Func<Task> recordsSaved;
    private readonly bool memberMode;
    private readonly ComboBox factor=new(){Name="LocalRouteFactor",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly ComboBox baseline=new(){Name="LocalRouteBaseline",ItemsSource=new[]{"自动参考","G","F","E","D","C","B","A"},SelectedIndex=0};
    private readonly ComboBox targetRank=new(){Name="LocalRouteTargetRank",ItemsSource=new[]{"G","F","E","D","C","B","A","S"},SelectedIndex=6};
    private readonly ComboBox targetVariant=new(){Name="LocalRouteTargetVariant",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly TextBlock scope=Note("本地资料参考；请先在上方选择规划账号与育成目标。"),status=Note("先选择成品或待培养角色，再核对路线。"),aptitude=Note("适性缺星尚未核对。"),ready=Note("");
    private readonly StackPanel results=new(){Name="LocalRouteResults",Spacing=6};
    private readonly PedigreeNodeCard targetCard=new("target","育成目标");
    private readonly ContentControl targetCharacterEditor=new(){Name="PedigreeTargetCharacterEditor"};
    private readonly StackPanel targetInspector=new(){Name="PedigreeTargetInspector",Spacing=8};
    private readonly StackPanel editorPool=new(){Name="PedigreeEditorPool",IsVisible=false};
    private readonly ContentControl inspectorContent=new(){Name="PedigreeInspectorContent",HorizontalContentAlignment=HorizontalAlignment.Stretch};
    private readonly TextBlock inspectorTitle=Note("选择一个节点查看或编辑");
    private readonly Border inspector=new(){Name="PedigreeInspector",Padding=new Thickness(12),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(10),IsVisible=false};
    private readonly Grid workspace=new(){Name="PedigreeWorkspace",RowSpacing=10,ColumnSpacing=14};
    private readonly PedigreeGraph graph;
    private readonly ParentPlanningAiPanel aiPanel;
    private readonly Slot[] slots;
    private readonly Button evaluate=new(){Name="LocalRouteEvaluate",Content="核对本地路线、胜场与缺星"};
    private readonly Dictionary<string,JObject> bindings=new();
    private JObject[] characters=[],entries=[];private JObject? cachedDraft;private bool loading,busy,catalogLoaded;private int generation;private string priorContext="",catalogueLibraryId="";
    private bool ForeignLibrary=>Str(context()["library_id"]) is string selected&&selected.Length>0&&(Str(context()["current_library_id"]) is string current&&current.Length>0?selected!=current:catalogueLibraryId.Length>0&&selected!=catalogueLibraryId);
    public bool HasManualReferences=>slots.Any(s=>s.IsManualReference);
    public event Action? DraftChanged;
    public ParentLocalRouteView(Func<JObject,Task<JObject>> request,Func<JObject> context,Func<string?[]> familyParents,Func<Task> recordsSaved,bool memberMode=false)
    {
        this.request=request;this.context=context;this.familyParents=familyParents;this.recordsSaved=recordsSaved;this.memberMode=memberMode;
        Name="ParentLocalRoute";Spacing=10;Margin=new Thickness(0,8,0,0);
        this.Bind(TextElement.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        this.Bind(BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        scope.IsVisible=!memberMode;Children.Add(scope);var instructions=Note("点击节点编辑角色、材料或实际胜场。成品祖辈保持真实快照；更换祖辈前，先将亲代明确改为待培养。");instructions.IsVisible=!memberMode;Children.Add(instructions);if(memberMode)workspace.MaxWidth=1160;
        var import=new Button{Name="LocalRouteImportFamily",Content="从已载入家系带入成品",IsVisible=!memberMode};import.Click+=(_,_)=>{ImportFamily();DraftChanged?.Invoke();};Children.Add(import);
        var goal=new WrapPanel();goal.Children.Add(Field("适性目标",factor));goal.Children.Add(Field("当前基准等级",baseline));goal.Children.Add(Field("开局目标等级",targetRank));
        targetInspector.Children.Add(targetCharacterEditor);targetInspector.Children.Add(goal);targetInspector.Children.Add(Field("育成目标衣装（用于自动基准参考）",targetVariant));targetInspector.Children.Add(Note("自动基准来自本地资料参考。衣装冲突时请明确选择或手动填写；S 会提示开局不可达。"));editorPool.Children.Add(targetInspector);
        slots=Enumerable.Range(0,6).Select(i=>new Slot(this,i)).ToArray();
        foreach(var slot in slots)editorPool.Children.Add(slot.EditorView);graph=new PedigreeGraph(targetCard,slots.Cast<Control>().ToArray());workspace.Children.Add(graph);
        var inspectorBody=new StackPanel{Spacing=8};var close=new Button{Name="PedigreeCloseInspector",Content="收起节点编辑"};close.Click+=(_,_)=>HideInspector();inspectorBody.Children.Add(inspectorTitle);inspectorBody.Children.Add(close);inspectorBody.Children.Add(inspectorContent);inspector.Child=inspectorBody;inspector.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));inspector.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));workspace.Children.Add(inspector);Children.Add(workspace);Children.Add(editorPool);
        aiPanel=new ParentPlanningAiPanel(request,AiContext,ApplyDraft);
        Children.Add(evaluate);Children.Add(status);Children.Add(aptitude);Children.Add(ready);Children.Add(new Expander{Name="LocalRouteResultDetails",Header="详细复核与培养顺序",Content=results,IsExpanded=false});aiPanel.IsVisible=!memberMode;Children.Add(aiPanel);
        targetCard.Click+=(_,_)=>ShowTargetEditor();
        evaluate.Click+=async(_,_)=>await Evaluate();foreach(var box in new[]{factor,baseline,targetRank,targetVariant})box.SelectionChanged+=(_,_)=>Changed();
        SizeChanged+=(_,_)=>LayoutInspector();
        factor.ItemsSource=new[]{new Choice("red:泥地","泥地")};factor.SelectedIndex=0;SetEntries([]);
    }
    private static TextBlock Note(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=12};
    private static Control Field(string title,Control body)=>new StackPanel{Spacing=4,Margin=new Thickness(0,0,10,6),Children={Note(title),body}};
    private static string Str(JToken? value)=>value?.Type is JTokenType.String or JTokenType.Integer?value.ToString():"";
    private static int? Number(JToken? value)=>value?.Type==JTokenType.Integer?(int?)value:null;
    private static string RaceSummaryLabel(JObject? value)
    {
        if(value==null)return "未录";var known=Number(value["known_win_count"]);var unverified=Number(value["unverified_count"])??0;
        if((bool?)value["complete"]==true)return known is int count?$"已确认 {count} 场（完整）":"记录完整";
        if(Str(value["coverage"])=="unknown"&&known==null)return "未录 / 未确认";
        return (known is int n?$"已确认 {n} 场":"已知胜场未确认")+(unverified>0?$"，另有 {unverified} 场待核对（未确认完整）":"（清单未齐）");
    }
    private static string CharacterLabel(JObject row)=>Str(row["name"]) is string name&&name.Length>0?name:"角色 #"+Str(row["character_id"]);
    private void Changed(){if(loading)return;generation++;bindings.Clear();foreach(var slot in slots)slot.ClearEvidence();aptitude.Text="方案已修改，适性与胜场尚未重新核对。";ready.Text="";results.Children.Clear();cachedDraft=null;ApplyLocks();RefreshTargetCard();aiPanel?.ContextChanged();DraftChanged?.Invoke();}
    private JObject AiContext(){var ctx=context();return new JObject{["target_character_id"]=ctx["target_character_id"]?.DeepClone(),["library_id"]=ctx["library_id"]?.DeepClone(),["account_id"]=ctx["account_id"]?.DeepClone(),["include_unassigned"]=(bool?)ctx["include_unassigned"]==true,["local_route"]=ReadDraft()};}
    public void SetTargetEditor(Control editor){if(targetCharacterEditor.Content is Control prior)targetCharacterEditor.Content=null;targetCharacterEditor.Content=editor;editor.IsVisible=true;}
    public void ShowTargetEditor(){ShowInspector(targetInspector,"育成目标与适性");targetCard.Select(true);foreach(var slot in slots)slot.Select(false);}
    private void ShowNode(int index){ShowInspector(slots[index].EditorView,Titles[index]);targetCard.Select(false);for(var i=0;i<slots.Length;i++)slots[i].Select(i==index);}
    private void ShowInspector(Control editor,string title){if(inspectorContent.Content is Control previous){inspectorContent.Content=null;previous.IsVisible=false;editorPool.Children.Add(previous);}editorPool.Children.Remove(editor);editor.IsVisible=true;inspectorContent.Content=editor;inspectorTitle.Text=title;inspector.IsVisible=true;LayoutInspector();if(Bounds.Width<(memberMode?1180:1320))Dispatcher.UIThread.Post(()=>{if(inspector.IsVisible)inspector.BringIntoView();});}
    public void CloseInspector()=>HideInspector();
    private void HideInspector(){if(inspectorContent.Content is Control previous){inspectorContent.Content=null;previous.IsVisible=false;editorPool.Children.Add(previous);}inspector.IsVisible=false;targetCard.Select(false);foreach(var slot in slots)slot.Select(false);LayoutInspector();Dispatcher.UIThread.Post(()=>graph.BringIntoView());}
    private void LayoutInspector(){var side=inspector.IsVisible&&Bounds.Width>=(memberMode?1180:1320);workspace.ColumnDefinitions=new(side?(memberMode?"*,320":"*,360"):"*");workspace.RowDefinitions=new(side?"Auto":"Auto,Auto");Grid.SetColumn(graph,0);Grid.SetRow(graph,0);Grid.SetColumn(inspector,side?1:0);Grid.SetRow(inspector,side?0:1);}
    private void RefreshTargetCard(){var row=characters.FirstOrDefault(x=>Number(x["character_id"])==Number(context()["target_character_id"]));var variant=(targetVariant.SelectedItem as Choice)?.Id??"";targetCard.SetIdentity(row==null?"点击选择育成目标":CharacterLabel(row),OutfitName(row,variant),Portrait(row,variant));targetCard.SetState("本次育成目标");targetCard.SetRed(null,true,planned:((factor.SelectedItem as Choice)?.Label??"泥地")+" · "+(baseline.SelectedIndex==0?"自动参考":baseline.SelectedItem)+" → "+targetRank.SelectedItem);}
    private static string OutfitName(JObject? character,string variant){if(variant.Length==0)return "衣装未指定";var outfit=(character?["outfits"] as JArray??new()).OfType<JObject>().FirstOrDefault(x=>Str(x["variant_id"])==variant);var name=Str(outfit?["name"]);return name.Length>0?name:"衣装资料待核对";}
    private static string Portrait(JObject? character,string variant){var values=(character?["outfits"] as JArray??new()).OfType<JObject>();var id=variant.Length>0?variant:Str(values.FirstOrDefault()?["variant_id"]);if(id.Length!=6||!id.All(char.IsDigit))return "";var path=System.IO.Path.Combine(AppContext.BaseDirectory,"resource","portraits",id+".png");return System.IO.File.Exists(path)?path:"";}
    private void ApplyLocks(){foreach(var side in new[]{0,3}){var locked=slots[side].Kind=="entry";slots[side].SetLocked(false);slots[side+1].SetLocked(locked);slots[side+2].SetLocked(locked);}}
    public void SetEntries(JObject[] values)
    {
        var entriesChanged=!JToken.DeepEquals(new JArray(entries),new JArray(values));entries=values;foreach(var slot in slots)slot.RefreshEntries();var next=context();scope.Text="当前规划："+Str(next["account_label"])+"；已有 / 可借状态由账号资料核对。";
        var key=next.ToString();if(key!=priorContext||entriesChanged){priorContext=key;UpdateVariants();Changed();}
    }
    private void UpdateVariants()
    {
        var current=(targetVariant.SelectedItem as Choice)?.Id;var id=Number(context()["target_character_id"]);var row=characters.FirstOrDefault(c=>Number(c["character_id"])==id);
        var values=new[]{new Choice("","自动参考（未指定衣装）")}.Concat((row?["outfits"] as JArray??new()).OfType<JObject>().Select(o=>new Choice(Str(o["variant_id"]),Str(o["name"])+" · "+Str(o["variant_id"]),o))).ToArray();
        var was=loading;loading=true;try{targetVariant.ItemsSource=values;targetVariant.SelectedItem=values.FirstOrDefault(v=>v.Id==current)??values[0];}finally{loading=was;}
    }
    public async Task LoadCatalog()
    {
        if(catalogLoaded)return;try{var data=await request(new JObject{["action"]="parent_planning_route_catalog"});loading=true;
            characters=(data["characters"] as JArray??new()).OfType<JObject>().ToArray();catalogueLibraryId=Str(data["library_id"]);var values=(data["aptitude_factors"] as JArray??new()).OfType<JObject>().Select(x=>new Choice(Str(x["factor_key"]),Str(x["label"]),x)).ToArray();
            factor.ItemsSource=values.Length>0?values:new[]{new Choice("red:泥地","泥地")};factor.SelectedItem=((IEnumerable<Choice>)factor.ItemsSource).FirstOrDefault(x=>x.Id=="red:泥地")??((IEnumerable<Choice>)factor.ItemsSource).First();
            foreach(var slot in slots)slot.RefreshCharacters();UpdateVariants();catalogLoaded=true;loading=false;if(cachedDraft!=null)LoadDraft(cachedDraft);RefreshTargetCard();
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="本地路线目录暂不可用；保留草稿，请稍后刷新。";}finally{loading=false;}
    }
    public JObject ReadDraft()
    {
        if(!catalogLoaded&&cachedDraft!=null)return (JObject)cachedDraft.DeepClone();
        var rows=new JArray();for(var i=0;i<6;i++)rows.Add(i%3!=0&&slots[i/3*3].Kind=="entry"?JValue.CreateNull():slots[i].Read()??JValue.CreateNull());
        return new JObject{["aptitude_goal"]=new JObject{["factor_key"]=(factor.SelectedItem as Choice)?.Id??"red:泥地",["baseline_rank"]=baseline.SelectedIndex==0?JValue.CreateNull():new JValue((string?)baseline.SelectedItem),["target_rank"]=(string?)targetRank.SelectedItem??"A",["target_variant_id"]=StrId(targetVariant)},["nodes"]=rows};
    }
    private static JToken StrId(ComboBox box)=>box.SelectedItem is Choice c&&c.Id.Length>0?new JValue(c.Id):JValue.CreateNull();
    public void LoadDraft(JObject? value)
    {
        var draft=value==null?new JObject{["aptitude_goal"]=new JObject{["factor_key"]="red:泥地",["baseline_rank"]=null,["target_rank"]="A",["target_variant_id"]=null},["nodes"]=new JArray(Enumerable.Repeat<JToken>(JValue.CreateNull(),6))}:(JObject)value.DeepClone();cachedDraft=draft;
        loading=true;try{var goal=draft["aptitude_goal"] as JObject??new();var factors=factor.ItemsSource as IEnumerable<Choice>??[];factor.SelectedItem=factors.FirstOrDefault(x=>x.Id==Str(goal["factor_key"]))??factors.FirstOrDefault();baseline.SelectedIndex=Array.IndexOf(new[]{"","G","F","E","D","C","B","A"},Str(goal["baseline_rank"]));if(baseline.SelectedIndex<0)baseline.SelectedIndex=0;targetRank.SelectedItem=Str(goal["target_rank"]) is string rank&&rank.Length>0?rank:"A";UpdateVariants();var variants=targetVariant.ItemsSource as IEnumerable<Choice>??[];var vid=Str(goal["target_variant_id"]);if(vid.Length>0&&variants.All(v=>v.Id!=vid)){var retained=new Choice(vid,"原衣装 #"+vid+"（资料待核对）");variants=variants.Append(retained).ToArray();targetVariant.ItemsSource=variants;}targetVariant.SelectedItem=variants.FirstOrDefault(x=>x.Id==vid)??variants.FirstOrDefault();var nodes=draft["nodes"] as JArray??new();for(var i=0;i<6;i++){slots[i].ReferenceCharacter=null;slots[i].Load(nodes.ElementAtOrDefault(i) as JObject);}}finally{loading=false;}generation++;bindings.Clear();foreach(var slot in slots)slot.ClearEvidence();ApplyLocks();RefreshTargetCard();aiPanel.ContextChanged();results.Children.Clear();aptitude.Text="已载入本地路线草稿，尚未核对。";ready.Text="";
    }
    public void ApplyDraft(JObject value){LoadDraft(value);DraftChanged?.Invoke();}
    private void ImportFamily(){var ids=familyParents();LoadDraft(new JObject{["aptitude_goal"]=ReadDraft()["aptitude_goal"]!.DeepClone(),["nodes"]=new JArray(Enumerable.Range(0,6).Select(i=>i==0||i==3?ids.ElementAtOrDefault(i/3) is string id?(JToken)new JObject{["kind"]="entry",["entry_id"]=id}:JValue.CreateNull():JValue.CreateNull()))});status.Text="已带入当前两匹成品；祖辈核对时由真实记录展开，不复制手工改造系谱。";}
    public void ImportSavedFamily(string?[] parentIds,int?[] lineage,bool manualReference)
    {
        if(!manualReference){LoadDraft(new JObject{["aptitude_goal"]=ReadDraft()["aptitude_goal"]!.DeepClone(),["nodes"]=new JArray(Enumerable.Range(0,6).Select(i=>(i==0||i==3)&&parentIds.ElementAtOrDefault(i/3) is string id?(JToken)new JObject{["kind"]="entry",["entry_id"]=id}:JValue.CreateNull()))});}
        else{LoadDraft(null);for(var i=0;i<6;i++){slots[i].ReferenceCharacter=lineage.ElementAtOrDefault(i);slots[i].ClearEvidence();}status.Text="已载入原手动家系作为参考；尚未关联成品的节点不计入已确认材料。";}
    }
    public int?[] LegacyLineage()
    {
        var values=slots.Select(s=>s.CharacterId).ToArray();foreach(var side in new[]{0,3})if(slots[side].Read() is JObject spec&&Str(spec["kind"])=="entry"){var entry=ForeignLibrary?null:entries.FirstOrDefault(e=>Str(e["entry_id"])==Str(spec["entry_id"]));if(entry!=null){values[side]=Number(entry["character_id"]);var parents=entry["parents"] as JArray;values[side+1]=Number((parents?.ElementAtOrDefault(0) as JObject)?["character_id"]);values[side+2]=Number((parents?.ElementAtOrDefault(1) as JObject)?["character_id"]);}}return values;
    }
    public void RetainSavedLineageHints(int?[] hints){for(var i=0;i<6;i++)if(slots[i].CharacterId==null&&hints.ElementAtOrDefault(i) is int id){slots[i].ReferenceCharacter=id;slots[i].ClearEvidence();}}
    public async Task Evaluate()
    {
        if(busy)return;var ctx=context();if(Number(ctx["target_character_id"])==null){status.Text="请先在上方选择育成目标。";return;}var at=generation;var draft=ReadDraft();busy=true;evaluate.IsEnabled=false;status.Text="正在核对本地路线…";
        try{var result=await request(new JObject{["action"]="parent_planning_route_evaluate",["target_character_id"]=ctx["target_character_id"]!.DeepClone(),["library_id"]=ctx["library_id"]?.DeepClone(),["account_id"]=ctx["account_id"]?.DeepClone(),["include_unassigned"]=(bool?)ctx["include_unassigned"]==true,["local_route"]=draft});if(at!=generation)return;Render(result);status.Text="已按当前账号、成品快照和已确认记录核对。";}
        catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="路线核对未完成；草稿已保留，请检查资料后重试。";}finally{busy=false;evaluate.IsEnabled=true;}
    }
    private void Render(JObject value)
    {
        bindings.Clear();foreach(var row in (value["route_nodes"] as JArray??new()).OfType<JObject>()){var index=Number(row["index"])??Array.IndexOf(Keys,Str(row["key"]));if(index<0||index>=6)continue;slots[index].Evidence(row);if(row["record_binding"] is JObject binding)bindings[Keys[index]]=(JObject)binding.DeepClone();}
        var aptitudeResult=value["aptitude"] as JObject??new();var plan=aptitudeResult["plan"] as JObject??new();var baselineResult=aptitudeResult["baseline"] as JObject??new();var state=Str(plan["status"]);var known=Number(plan["known_stars"]);var gap=Number(plan["additional_stars"]);var lowerGap=Number(plan["gap_from_confirmed"]);var unknown=Number(plan["unknown_nodes"])??(plan["unknown_nodes"] as JArray)?.Count??0;
        aptitude.Text=state switch{"unreachable_at_start"=>"开局不可达：当前目标超出开局适性上限，不显示普通缺星。","unknown_baseline"=>"基准适性未知：请指定准确衣装或手动基准等级。","satisfied"=>$"适性要求已满足 · 已确认红星 {known?.ToString()??"未知"}",_=>$"已确认红星 {known?.ToString()??"未知"} · "+(gap is int n?$"仍缺 {n} 星":"缺星尚不能确认")+(unknown>0?$" · 未确认节点 {unknown}":"")};
        if(unknown>0&&state is "satisfied" or "unknown_baseline" or "unreachable_at_start")aptitude.Text+=$" · 未确认节点 {unknown}";
        if(Str(baselineResult["status"])=="ambiguous_variant")aptitude.Text+="\n同角色不同衣装的基准不一致，请选择准确衣装或手动填写。";
        else if(Str(baselineResult["rank"]).Length>0)aptitude.Text+="\n基准："+(Str(baselineResult["status"])=="manual"?"手动提供 ":"资料参考 ")+Str(baselineResult["rank"]);
        if(gap==null&&lowerGap is int lower&&state!="unreachable_at_start"&&state!="unknown_baseline")aptitude.Text+=$"\n已确认部分距目标还差 {lower} 星；未知节点补齐前，不能当作最终缺星。";
        ready.Text=(bool?)value["ready_to_start"]==true?"本地资料已满足当前路线检查；请在游戏开始前自行核对。":"当前为规划草稿，尚未满足开局资料要求。";var issues=(value["selection_issues"] as JArray??new()).Select(Str).Where(s=>s.Length>0).ToArray();if(issues.Length>0)ready.Text+="\n需要调整："+string.Join(" ",issues.Take(2));results.Children.Clear();
        foreach(var issue in (value["selection_issues"] as JArray??new()).Select(Str).Where(s=>s.Length>0))results.Children.Add(Note("需要调整："+issue));
        foreach(var link in (value["race_links"] as JArray??new()).OfType<JObject>())results.Children.Add(Note(NodeTitle(Str(link["left"]))+" × "+NodeTitle(Str(link["right"]))+"：已知共同 G1 "+(Number(link["known_common_count"])?.ToString()??"未知")+((bool?)link["complete"]==true?"（记录完整）":"（记录未齐，仅为已知下限）")));
        foreach(var task in (value["tasks"] as JArray??new()).OfType<JObject>()){var target=task["red_target"] as JObject;var red=target==null?"":" · 计划目标 "+Str(target["factor_key"]).Replace("red:","")+" "+Str(target["stars"])+" 星（尚未获得）";results.Children.Add(Note("待培养："+Str(task["label"])+red+" · "+Str(task["reason"])));}
        foreach(var note in (value["notes"] as JArray??new()).Select(Str).Where(x=>x.Length>0))results.Children.Add(Note(note));
    }
    private static string NodeTitle(string key)=>Array.IndexOf(Keys,key) is int index&&index>=0?Titles[index]:"家系节点";
    private async Task EditRaces(int index)
    {
        if(!bindings.TryGetValue(Keys[index],out var binding))return;await EditRecordBinding(binding,Titles[index]);
    }
    public async Task EditRecordBinding(JObject binding,string title,Window? explicitOwner=null)
    {
        var owner=explicitOwner??TopLevel.GetTopLevel(this) as Window;if(owner==null)return;
        var entry=Str(binding["entry_id"]);var node=Str(binding["node"]);try{var data=await request(new JObject{["action"]="parent_planning_races_get",["entry_id"]=entry,["node"]=node});
            var window=new ParentLocalRouteWinsWindow(title,data["race_records"] as JObject,Number(data["library_revision"])??throw new InvalidOperationException("胜场版本未返回，不能保存。"),
                async query=>await request(new JObject{["action"]="parent_planning_race_catalog",["query"]=query,["limit"]=80}),
                async (records,revision)=>await request(new JObject{["action"]="parent_planning_races_save",["entry_id"]=entry,["node"]=node,["race_records"]=records,["expected_revision"]=revision}));
            if(await window.ShowDialog<bool>(owner)){await recordsSaved();await Evaluate();}
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="实际胜场暂不可读，未修改库记录。";}
    }
    private sealed class Slot : Border
    {
        private readonly ParentLocalRouteView owner;private readonly int index;private bool applying,locked;
        private readonly ComboBox kind=new(){ItemsSource=new[]{"未选","库内成品","待培养角色"},SelectedIndex=0};
        private readonly TextBox search=new(){Watermark="输入角色、衣装或记录编号筛选"};
        private readonly ComboBox selection=new(){HorizontalAlignment=HorizontalAlignment.Stretch};
        private readonly ComboBox variant=new(){HorizontalAlignment=HorizontalAlignment.Stretch};
        private readonly ComboBox redFactor=new(){HorizontalAlignment=HorizontalAlignment.Stretch};
        private readonly StackPanel redGoal=new(){Spacing=4};
        private readonly NumericUpDown stars=new(){Minimum=0,Maximum=3,Value=0,Increment=1,FormatString="0",Width=120};
        private readonly TextBlock state=Note("快照资料未齐"),detail=Note("");
        private readonly Button wins=new(){Content="编辑实际胜场",IsEnabled=false},rebreed=new(){Content="调整祖辈并规划重育",IsVisible=false};
        private readonly StackPanel editor=new(){Spacing=5};private JObject? retained;
        private readonly PedigreeNodeCard compact;
        private JObject? evidence;
        public StackPanel EditorView {get;}=new(){Spacing=7};
        public int? ReferenceCharacter {get;set;}
        public bool IsManualReference=>ReferenceCharacter!=null&&Kind==null&&!locked;
        public int? CharacterId=>Number(evidence?["character_id"])??Number((selection.SelectedItem as Choice)?.Row?["character_id"])??Number(retained?["character_id"])??ReferenceCharacter;
        public string? Kind=>kind.SelectedIndex==1?"entry":kind.SelectedIndex==2?"planned":null;
        public Slot(ParentLocalRouteView owner,int index)
        {
            this.owner=owner;this.index=index;Name="LocalRouteSlot_"+Keys[index];compact=new PedigreeNodeCard(Keys[index],Titles[index]);compact.Click+=(_,_)=>owner.ShowNode(index);Child=compact;
            kind.Name="LocalRouteKind_"+Keys[index];search.Name="LocalRouteSearch_"+Keys[index];selection.Name="LocalRouteChoice_"+Keys[index];variant.Name="LocalRouteVariant_"+Keys[index];stars.Name="LocalRouteStars_"+Keys[index];redFactor.Name="LocalRouteRedFactor_"+Keys[index];wins.Name="LocalRouteWins_"+Keys[index];rebreed.Name="LocalRouteRebreed_"+Keys[index];state.Name="LocalRouteState_"+Keys[index];
            EditorView.Name="PedigreeNodeEditor_"+Keys[index];EditorView.Children.Add(state);editor.Children.Add(kind);editor.Children.Add(search);editor.Children.Add(selection);editor.Children.Add(variant);redGoal.Children.Add(Note("计划培养的红因子；0 星表示未指定，不计入已确认红星"));redGoal.Children.Add(redFactor);redGoal.Children.Add(stars);editor.Children.Add(redGoal);EditorView.Children.Add(editor);EditorView.Children.Add(detail);EditorView.Children.Add(rebreed);EditorView.Children.Add(wins);
            kind.SelectionChanged+=(_,_)=>{if(applying)return;retained=null;RefreshChoices();owner.Changed();};search.TextChanged+=(_,_)=>RefreshChoices();selection.SelectionChanged+=(_,_)=>{if(applying)return;retained=null;RefreshVariants();if(index%3==0&&Kind=="entry"){owner.slots[index+1].ReferenceCharacter=null;owner.slots[index+2].ReferenceCharacter=null;}owner.Changed();};variant.SelectionChanged+=(_,_)=>{if(!applying)owner.Changed();};redFactor.SelectionChanged+=(_,_)=>{if(!applying)owner.Changed();};stars.ValueChanged+=(_,_)=>{if(!applying)owner.Changed();};wins.Click+=async(_,_)=>await owner.EditRaces(index);
            rebreed.Click+=(_,_)=>{var row=(selection.SelectedItem as Choice)?.Row;var character=Number(row?["character_id"]);Load(new JObject{["kind"]="planned",["character_id"]=character is int id?new JValue(id):JValue.CreateNull(),["variant_id"]=null,["red_target"]=null});owner.Changed();detail.Text="此槽已改为待培养；下面调整的是未来祖辈，原成品记录保持原样。";};RefreshChoices();
        }
        public void Select(bool selected)=>compact.Select(selected);
        public void SetLocked(bool value){var wasLocked=locked;locked=value;if(value&&!wasLocked)ReferenceCharacter=null;editor.IsVisible=!value;rebreed.IsVisible=index%3==0&&Kind=="entry";if(value&&state.Text=="快照资料未齐")state.Text="随成品亲代展开真实快照，核对后显示";RefreshCompact();}
        public void ClearEvidence(){evidence=null;wins.IsEnabled=false;state.Text=locked?"随成品亲代展开真实快照，待核对":Kind=="entry"&&owner.ForeignLibrary?"原成品来自另一份库，待重新关联":Kind=="planned"?"待培养（方案）":ReferenceCharacter!=null?"手动参考（未确认持有）":"快照资料未齐 / 账号状态待核对";detail.Text="";RefreshCompact();}
        public void RefreshEntries(){if(Kind=="entry")RefreshChoices();}
        public void RefreshCharacters(){if(Kind=="planned")RefreshChoices();RefreshCompact();}
        private void RefreshCompact()
        {
            var character=owner.characters.FirstOrDefault(x=>Number(x["character_id"])==CharacterId);var source=(selection.SelectedItem as Choice)?.Row;var vid=Str(evidence?["variant_id"]);if(vid.Length==0)vid=Kind=="entry"?Str(source?["variant_id"]):(variant.SelectedItem as Choice)?.Id??"";
            compact.SetIdentity(character!=null?CharacterLabel(character):Str(evidence?["display_name"]) is string display&&display.Length>0?display:ReferenceCharacter is int reference?"未收录角色 "+reference:"点击选择",OutfitName(character,vid),Portrait(character,vid));compact.SetState(state.Text??"状态待核对",locked);
            var red=evidence?["red_summary"] as JObject;if(Kind=="planned"){var target=(Read() as JObject)?["red_target"] as JObject;compact.SetRed(null,true,planned:target==null?"红因子目标未指定":"计划 "+Str(target["factor_key"]).Replace("red:","")+" "+Str(target["stars"])+" 星");}else compact.SetRed(Number(red?["known_stars"]),(bool?)red?["unknown"]!=false,Str(red?["conflict"]).Length>0);
        }
        private void RefreshChoices()
        {
            var current=(selection.SelectedItem as Choice)?.Id;var query=(search.Text??"").Trim();var pool=Kind=="entry"?(owner.ForeignLibrary?Array.Empty<JObject>():owner.entries):owner.characters;
            var values=pool.Select(row=>Kind=="entry"?new Choice(Str(row["entry_id"]),string.Join(" · ",new[]{Str(row["character_name"]),Str(row["variant_name"]),Str(row["entry_id"])}.Where(x=>x.Length>0)),row):new Choice(Str(row["character_id"]),CharacterLabel(row),row)).Where(c=>query.Length==0||c.Label.Contains(query,StringComparison.OrdinalIgnoreCase)||(c.Row?["aliases"] as JArray??new()).Any(a=>Str(a).Contains(query,StringComparison.OrdinalIgnoreCase))).ToList();
            var prior=selection.SelectedItem as Choice;if(prior!=null&&values.All(x=>x.Id!=prior.Id)){if(Kind=="entry"&&owner.ForeignLibrary)retained=new JObject{["kind"]="entry",["entry_id"]=prior.Id};else values.Add(prior);}var was=applying;applying=true;try{selection.ItemsSource=values;selection.SelectedItem=values.FirstOrDefault(x=>x.Id==current);selection.IsVisible=search.IsVisible=Kind!=null;variant.IsVisible=redGoal.IsVisible=Kind=="planned";RefreshVariants();RefreshRedFactors();}finally{applying=was;}
        }
        private void RefreshVariants(){var previous=(variant.SelectedItem as Choice)?.Id;var row=(selection.SelectedItem as Choice)?.Row;var values=new[]{new Choice("","衣装未指定")}.Concat((row?["outfits"] as JArray??new()).OfType<JObject>().Select(x=>new Choice(Str(x["variant_id"]),Str(x["name"])+" · "+Str(x["variant_id"]),x))).ToArray();var was=applying;applying=true;try{variant.ItemsSource=values;variant.SelectedItem=values.FirstOrDefault(x=>x.Id==previous)??values[0];}finally{applying=was;}}
        private void RefreshRedFactors(){var id=(redFactor.SelectedItem as Choice)?.Id??(owner.factor.SelectedItem as Choice)?.Id;var values=(owner.factor.ItemsSource as IEnumerable<Choice>??[]).ToArray();redFactor.ItemsSource=values;redFactor.SelectedItem=values.FirstOrDefault(x=>x.Id==id)??values.FirstOrDefault();}
        public JToken? Read(){if(Kind=="entry")return selection.SelectedItem is Choice selected?new JObject{["kind"]="entry",["entry_id"]=selected.Id}:retained?.DeepClone();if(Kind=="planned")return new JObject{["kind"]="planned",["character_id"]=int.TryParse((selection.SelectedItem as Choice)?.Id,out var id)?new JValue(id):retained?["character_id"]?.DeepClone()??JValue.CreateNull(),["variant_id"]=StrId(variant),["red_target"]=stars.Value>0?new JObject{["factor_key"]=(redFactor.SelectedItem as Choice)?.Id??"red:泥地",["stars"]=(int)(stars.Value??0)}:JValue.CreateNull()};return null;}
        public void Load(JObject? value){applying=true;try{retained=value==null?null:(JObject)value.DeepClone();kind.SelectedIndex=Str(value?["kind"]) switch{"entry"=>1,"planned"=>2,_=>0};search.Text="";selection.SelectedItem=null;RefreshChoices();var id=Kind=="entry"?Str(value?["entry_id"]):Str(value?["character_id"]);var values=selection.ItemsSource as IEnumerable<Choice>??[];selection.SelectedItem=values.FirstOrDefault(x=>x.Id==id);RefreshVariants();var vid=Str(value?["variant_id"]);var variants=variant.ItemsSource as IEnumerable<Choice>??[];if(vid.Length>0&&variants.All(x=>x.Id!=vid)){variants=variants.Append(new Choice(vid,"原衣装 #"+vid+"（待核对）")).ToArray();variant.ItemsSource=variants;}variant.SelectedItem=variants.FirstOrDefault(x=>x.Id==vid)??variants.FirstOrDefault();var red=value?["red_target"] as JObject;stars.Value=Number(red?["stars"])??0;var redChoices=redFactor.ItemsSource as IEnumerable<Choice>??[];redFactor.SelectedItem=redChoices.FirstOrDefault(x=>x.Id==Str(red?["factor_key"]))??redChoices.FirstOrDefault();}finally{applying=false;}ClearEvidence();}
        public void Evidence(JObject row){evidence=(JObject)row.DeepClone();state.Text=Str(row["status_label"]) is string label&&label.Length>0?label:"状态未确认";if(Kind==null&&ReferenceCharacter!=null)state.Text="手动参考（未确认持有）";var name=owner.characters.FirstOrDefault(x=>Number(x["character_id"])==Number(row["character_id"]));var race=row["race_summary"] as JObject;var red=row["red_summary"] as JObject;detail.Text=(name==null?"角色资料未齐":CharacterLabel(name))+" · 实际胜场 "+RaceSummaryLabel(race)+"\n已确认红星："+(Number(red?["known_stars"])?.ToString()??"未知")+((bool?)red?["unknown"]==true?"（仍有未知资料）":"")+(Str(red?["conflict"]).Length>0?"（记录冲突）":"");wins.IsEnabled=row["record_binding"] is JObject;foreach(var note in (row["notes"] as JArray??new()).Select(Str).Where(s=>s.Length>0))detail.Text+="\n"+note;RefreshCompact();}
    }
}
