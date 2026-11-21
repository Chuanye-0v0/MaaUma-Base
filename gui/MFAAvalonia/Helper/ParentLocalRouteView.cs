using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Local draft and read-only backend assessment; ownership is never inferred by this view.</summary>
public sealed class ParentLocalRouteView : StackPanel
{
    private static readonly string[] Keys=["parent_a","gp_a1","gp_a2","parent_b","gp_b1","gp_b2"];
    private static readonly string[] Titles=["亲代 A","祖辈 A1","祖辈 A2","亲代 B","祖辈 B1","祖辈 B2"];
    private static readonly string[] StartRanks=["G","F","E","D","C","B","A"];
    private static readonly string[] AllRanks=["G","F","E","D","C","B","A","S"];
    private static readonly int[] StarThresholds=[0,1,4,7,10];
    private sealed record AptitudeSpec(string FactorKey,string ProfileKey,string Label);
    private static readonly AptitudeSpec[] GridSpecs=
    [
        new("red:草地","turf","草地"), new("red:泥地","dirt","泥地"),
        new("red:短距离","short","短距离"), new("red:英里","mile","英里"),
        new("red:中距离","middle","中距离"), new("red:长距离","long","长距离"),
        new("red:领跑","nige","领跑"), new("red:跟前","senko","跟前"),
        new("red:居中","sashi","居中"), new("red:后追","oikomi","后追"),
    ];
    private sealed record Choice(string Id,string Label,JObject? Row=null){public override string ToString()=>Label;}
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<JObject> context;
    private readonly Func<string?[]> familyParents;
    private readonly Func<Task> recordsSaved;
    private readonly Func<JObject>? externalGoals;
    private readonly bool memberMode;
    private readonly ComboBox targetVariant=new(){Name="LocalRouteTargetVariant",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly StackPanel tenGridSection=new(){Name="LocalRouteTenGrid",Spacing=6};
    private readonly TextBlock tenGridBudget=Note("当前不改开局适性（保持先天等级开局）。");
    private readonly Button lockNonGoals=new(){Name="LocalRouteLockNonGoals",Content="🔒 一键锁非目标(防污染)",FontSize=12};
    private readonly Button clearGoals=new(){Name="LocalRouteClearAptitudeGoals",Content="清空十格设置",FontSize=12};
    private readonly AptitudeCell[] cells;
    private readonly Dictionary<string,ToggleButton> chaseSToggles=new(StringComparer.Ordinal);
    private readonly TextBlock chaseSSummaryText=Note("局中追 S 目标（不占开局 6 槽名额；靠双亲+祖辈叠加红星在继承事件触发 S）。");
    private Choice[] redFactorChoices;
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
    private readonly ParentExternalRoutePanel externalPanel;
    private readonly AutoPairPanel autoPairPanel;
    private readonly Slot[] slots;
    private readonly Button evaluate=new(){Name="LocalRouteEvaluate",Content="核对本地路线、胜场与缺星"};
    private readonly Dictionary<string,JObject> bindings=new();
    private JObject[] characters=[],entries=[],aptitudeProfiles=[];
    private JObject? cachedDraft;private bool loading,busy,catalogLoaded;private int generation;private string priorContext="",catalogueLibraryId="";
    private bool ForeignLibrary=>Str(context()["library_id"]) is string selected&&selected.Length>0&&(Str(context()["current_library_id"]) is string current&&current.Length>0?selected!=current:catalogueLibraryId.Length>0&&selected!=catalogueLibraryId);
    public bool HasManualReferences=>slots.Any(s=>s.IsManualReference);
    public event Action? DraftChanged;
    public ParentLocalRouteView(Func<JObject,Task<JObject>> request,Func<JObject> context,Func<string?[]> familyParents,
        Func<Task> recordsSaved,bool memberMode=false,Func<JObject>? externalGoals=null)
    {
        this.request=request;this.context=context;this.familyParents=familyParents;this.recordsSaved=recordsSaved;this.memberMode=memberMode;
        this.externalGoals=externalGoals;
        redFactorChoices=GridSpecs.Select(x=>new Choice(x.FactorKey,x.Label)).ToArray();
        Name="ParentLocalRoute";Spacing=10;Margin=new Thickness(0,8,0,0);
        this.Bind(TextElement.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        this.Bind(BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        scope.IsVisible=!memberMode;Children.Add(scope);var instructions=Note("点击节点编辑角色、材料、1~3★目标或第4/5层红星预算。成品祖辈保持真实快照；更换祖辈前，先将亲代改为待培养。");instructions.IsVisible=!memberMode;Children.Add(instructions);if(memberMode)workspace.MaxWidth=1480;
        var import=new Button{Name="LocalRouteImportFamily",Content="从已载入家系带入成品",IsVisible=!memberMode};import.Click+=(_,_)=>{ImportFamily();DraftChanged?.Invoke();};Children.Add(import);
        Children.Add(BuildChaseSBar());

        cells=GridSpecs.Select(spec=>new AptitudeCell(this,spec)).ToArray();
        BuildTenGridUi();
        targetInspector.Children.Add(targetCharacterEditor);
        targetInspector.Children.Add(Field("育成目标衣装（用于自动基准参考）",targetVariant));
        targetInspector.Children.Add(tenGridSection);
        targetInspector.Children.Add(Note("十格适性表来自本地先天适性参考（开局最高只可改到 A；若要追 S，请点上方『局中追 S 目标』）。"));
        editorPool.Children.Add(targetInspector);

        slots=Enumerable.Range(0,6).Select(i=>new Slot(this,i)).ToArray();
        foreach(var slot in slots)editorPool.Children.Add(slot.EditorView);graph=new PedigreeGraph(targetCard,slots.Cast<Control>().ToArray());graph.AncestorSlotClicked+=ShowNode;workspace.Children.Add(graph);
        var inspectorBody=new StackPanel{Spacing=8};var close=new Button{Name="PedigreeCloseInspector",Content="收起节点编辑"};close.Click+=(_,_)=>HideInspector();inspectorBody.Children.Add(inspectorTitle);inspectorBody.Children.Add(close);inspectorBody.Children.Add(inspectorContent);inspector.Child=inspectorBody;inspector.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));inspector.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));workspace.Children.Add(inspector);Children.Add(workspace);Children.Add(editorPool);
        aiPanel=new ParentPlanningAiPanel(request,AiContext,ApplyDraft);
        externalPanel=new ParentExternalRoutePanel(request,ExternalContext,
            (draft,review)=>{ApplyDraft(draft);Render(review);},
            index=>index%3==0||!slots[index/3*3].IsFinished,
            OnExternalPoolChanged);
        autoPairPanel=new AutoPairPanel(request,ExternalContext,
            (draft,review)=>{ApplyDraft(draft);Render(review);},
            OnAutoPairPoolMutated,DescribeLockedSlots);
        Children.Add(evaluate);Children.Add(status);Children.Add(aptitude);Children.Add(ready);Children.Add(autoPairPanel);Children.Add(externalPanel);Children.Add(new Expander{Name="LocalRouteResultDetails",Header="详细复核与培养顺序",Content=results,IsExpanded=false});aiPanel.IsVisible=!memberMode;Children.Add(aiPanel);
        targetCard.Click+=(_,_)=>ShowTargetEditor();
        evaluate.Click+=async(_,_)=>await Evaluate();
        targetVariant.SelectionChanged+=(_,_)=>{if(loading)return;RefreshTenGridBaselines(clearExceeded:true);Changed();};
        lockNonGoals.Click+=(_,_)=>LockNonGoalAptitudes();
        clearGoals.Click+=(_,_)=>ClearAllGoals();
        SizeChanged+=(_,_)=>LayoutInspector();
        SetEntries([]);RefreshTenGridBaselines(clearExceeded:false);RefreshTargetCard();RefreshVisualCards();
    }

    private Control BuildChaseSBar()
    {
        var specs=new (string Key,string Id,string Label)[]
        {
            ("red:中距离","middle","中S"),("red:泥地","dirt","泥S"),("red:英里","mile","英S"),("red:长距离","long","长S"),("red:短距离","short","短S"),
            ("red:草地","turf","草S"),("red:领跑","nige","逃S"),("red:跟前","senko","先S"),("red:居中","sashi","差S"),("red:后追","oikomi","追S")
        };
        var row=new WrapPanel{VerticalAlignment=VerticalAlignment.Center};
        var title=Note("🎯 局中追 S 目标（不占开局 6 槽）：");
        title.FontWeight=FontWeight.SemiBold;title.Margin=new Thickness(0,2,8,4);title.VerticalAlignment=VerticalAlignment.Center;
        row.Children.Add(title);
        foreach(var s in specs)
        {
            var btn=new ToggleButton{Name="LocalRouteChaseS_"+s.Id,Content=s.Label,FontSize=11,Padding=new Thickness(8,3),Margin=new Thickness(0,0,6,4),CornerRadius=new CornerRadius(6)};
            btn.IsCheckedChanged+=(_,_)=>{if(!loading)Changed();};
            chaseSToggles[s.Key]=btn;
            row.Children.Add(btn);
        }
        var box=new StackPanel{Spacing=4,Children={row,chaseSSummaryText}};
        var bar=new Border{Name="LocalRouteChaseSBar",Padding=new Thickness(10,7),CornerRadius=new CornerRadius(10),BorderThickness=new Thickness(1),Child=box};
        bar.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));
        bar.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        return bar;
    }

    private static string ShortFactorTag(string fk)
    {
        var key=fk.StartsWith("red:",StringComparison.Ordinal)?fk[4..]:fk.StartsWith("pink:",StringComparison.Ordinal)?fk[5..]:fk;
        return key switch
        {
            "草地" or "turf"=>"草","泥地" or "dirt"=>"泥","短距离" or "short"=>"短","英里" or "mile"=>"英","中距离" or "middle"=>"中",
            "长距离" or "long"=>"长","领跑" or "nige"=>"逃","跟前" or "senko"=>"先","居中" or "sashi"=>"差","后追" or "oikomi"=>"追",
            _=>key
        };
    }

    private static string? ConfirmedStartRank(string? baseRank,int stars)
    {
        var bi=baseRank==null?-1:Array.IndexOf(StartRanks,baseRank);
        if(bi<0)return null;
        var raises=stars>=10?4:stars>=7?3:stars>=4?2:stars>=1?1:0;
        return StartRanks[Math.Min(6,bi+raises)];
    }

    private static void AccumulateRedList(JArray? factors,Dictionary<string,int> dest)
    {
        if(factors==null)return;
        foreach(var f in factors.OfType<JObject>())
        {
            if(Str(f["kind"]) is {Length:>0} k&&k!="red")continue;
            var fk=Str(f["factor_key"]);
            if(fk.StartsWith("pink:",StringComparison.Ordinal))fk="red:"+fk[5..];
            var st=Number(f["stars"])??0;
            if(fk.Length>0&&st>=1&&st<=3)dest[fk]=dest.GetValueOrDefault(fk)+st;
        }
    }

    private static JObject? MakeRedBadgeObj(string fk,int stars)
    {
        if(string.IsNullOrWhiteSpace(fk)||stars<=0)return null;
        var label=fk.Contains(':')?fk.Split(':',2)[1]:fk;
        return new JObject{["factor_key"]=fk,["label"]=label,["short_label"]=ShortFactorTag(fk),["stars"]=stars};
    }

    private JArray? BuildClientAptitudeGrid(int? charId,string? vid,Dictionary<string,int>? confStars,Dictionary<string,int>? planStars,Dictionary<string,string>? manualBaselines=null)
    {
        var matches=charId!=null?aptitudeProfiles.Where(r=>Number(r["character_id"])==charId&&(string.IsNullOrEmpty(vid)||Str(r["variant_id"])==vid)).ToArray():[];
        if(matches.Length==0&&(manualBaselines==null||manualBaselines.Count==0))return null;
        var grid=new JArray();
        var anyKnown=false;
        foreach(var spec in GridSpecs)
        {
            string? baseRank=null;
            if(manualBaselines!=null&&manualBaselines.TryGetValue(spec.FactorKey,out var man)&&Array.IndexOf(StartRanks,man)>=0)
                baseRank=man;
            else if(matches.Length>0)
            {
                var distinct=matches.Select(r=>Str((r["aptitudes"] as JObject)?[spec.ProfileKey])).Where(r=>Array.IndexOf(StartRanks,r)>=0).Distinct().ToArray();
                if(distinct.Length==1)baseRank=distinct[0];
                else if(distinct.Length>1)baseRank=distinct.OrderBy(r=>Array.IndexOf(StartRanks,r)).First();
            }
            if(baseRank!=null)anyKnown=true;
            var conf=confStars?.GetValueOrDefault(spec.FactorKey,0)??0;
            var plan=planStars?.GetValueOrDefault(spec.FactorKey,0)??0;
            var eff=ConfirmedStartRank(baseRank,conf);
            var planRank=ConfirmedStartRank(baseRank,conf+plan);
            grid.Add(new JObject
            {
                ["factor_key"]=spec.FactorKey,
                ["profile_key"]=spec.ProfileKey,
                ["label"]=spec.Label,
                ["short_label"]=ShortFactorTag(spec.FactorKey),
                ["baseline_rank"]=baseRank!=null?new JValue(baseRank):JValue.CreateNull(),
                ["effective_rank"]=eff!=null?new JValue(eff):JValue.CreateNull(),
                ["planned_rank"]=planRank!=null?new JValue(planRank):JValue.CreateNull(),
                ["confirmed_stars"]=conf,
                ["planned_stars"]=plan,
                ["upgraded"]=eff!=null&&baseRank!=null&&eff!=baseRank,
                ["planned_upgraded"]=planRank!=null&&baseRank!=null&&planRank!=baseRank
            });
        }
        return anyKnown?grid:null;
    }

    private static string? FormatUpgradeSubBadge(JArray? grid)
    {
        if(grid==null)return null;
        var parts=new List<string>();
        foreach(var item in grid.OfType<JObject>())
        {
            var baseR=Str(item["baseline_rank"]);
            var planR=Str(item["planned_rank"]);
            var effR=Str(item["effective_rank"]);
            var targetR=planR.Length>0?planR:effR;
            if(baseR.Length>0&&targetR.Length>0&&targetR!=baseR&&((bool?)item["upgraded"]==true||(bool?)item["planned_upgraded"]==true))
                parts.Add($"{Str(item["short_label"])}{baseR}→{targetR}");
        }
        if(parts.Count==0)return null;
        return parts.Count<=2?string.Join(" ",parts):string.Join(" ",parts.Take(2))+$" +{parts.Count-2}";
    }

    private void RefreshVisualCards()
    {
        if(graph==null!)return;
        var l4ByGp=new Dictionary<int,JArray>();
        foreach(var gi in new[]{1,2,4,5})
        {
            var (l4,l5)=slots[gi].BuildClientAncestors(slots[gi/3*3].IsFinished);
            l4ByGp[gi]=l4;
            graph.SetAncestorData(gi,l4,l5);
            var gpConf=new Dictionary<string,int>(StringComparer.Ordinal);
            var gpPlan=new Dictionary<string,int>(StringComparer.Ordinal);
            foreach(var item in l4.OfType<JObject>().Concat(l5.OfType<JObject>()))
            {
                if(item["red_factor"] is not JObject rf)continue;
                var fk=Str(rf["factor_key"]);var st=Number(rf["stars"])??0;
                if(fk.Length==0||st<=0)continue;
                if(Str(item["source"])=="library_snapshot")gpConf[fk]=gpConf.GetValueOrDefault(fk)+st;
                else if(Str(item["source"])=="planned_budget")gpPlan[fk]=gpPlan.GetValueOrDefault(fk)+st;
            }
            var gpGrid=BuildClientAptitudeGrid(slots[gi].CharacterId,slots[gi].SelectedVariantId,gpConf,gpPlan);
            slots[gi].ApplyClientVisuals(gpGrid);
        }
        foreach(var pi in new[]{0,3})
        {
            var pConf=new Dictionary<string,int>(StringComparer.Ordinal);
            var pPlan=new Dictionary<string,int>(StringComparer.Ordinal);
            foreach(var ci in new[]{pi+1,pi+2})
            {
                slots[ci].AccumulateDirectRedStars(pConf,pPlan);
                if(l4ByGp.TryGetValue(ci,out var l4Arr))
                {
                    foreach(var item in l4Arr.OfType<JObject>())
                    {
                        if(item["red_factor"] is not JObject rf)continue;
                        var fk=Str(rf["factor_key"]);var st=Number(rf["stars"])??0;
                        if(fk.Length==0||st<=0)continue;
                        if(Str(item["source"])=="library_snapshot")pConf[fk]=pConf.GetValueOrDefault(fk)+st;
                        else if(Str(item["source"])=="planned_budget")pPlan[fk]=pPlan.GetValueOrDefault(fk)+st;
                    }
                }
            }
            var pGrid=BuildClientAptitudeGrid(slots[pi].CharacterId,slots[pi].SelectedVariantId,pConf,pPlan);
            slots[pi].ApplyClientVisuals(pGrid);
        }
        var tConf=new Dictionary<string,int>(StringComparer.Ordinal);
        var tPlan=new Dictionary<string,int>(StringComparer.Ordinal);
        var pTot=new Dictionary<string,int>(StringComparer.Ordinal);
        var gTot=new Dictionary<string,int>(StringComparer.Ordinal);
        for(var i=0;i<6;i++)
        {
            var subConf=new Dictionary<string,int>(StringComparer.Ordinal);
            var subPlan=new Dictionary<string,int>(StringComparer.Ordinal);
            slots[i].AccumulateDirectRedStars(subConf,subPlan);
            foreach(var kv in subConf){tConf[kv.Key]=tConf.GetValueOrDefault(kv.Key)+kv.Value;if(i is 0 or 3)pTot[kv.Key]=pTot.GetValueOrDefault(kv.Key)+kv.Value;else gTot[kv.Key]=gTot.GetValueOrDefault(kv.Key)+kv.Value;}
            foreach(var kv in subPlan){tPlan[kv.Key]=tPlan.GetValueOrDefault(kv.Key)+kv.Value;if(i is 0 or 3)pTot[kv.Key]=pTot.GetValueOrDefault(kv.Key)+kv.Value;else gTot[kv.Key]=gTot.GetValueOrDefault(kv.Key)+kv.Value;}
        }
        var tManual=cells.Where(c=>c.ManualRank!=null).ToDictionary(c=>c.FactorKey,c=>c.ManualRank!,StringComparer.Ordinal);
        var tVid=Str(StrId(targetVariant));
        var tGrid=BuildClientAptitudeGrid(Number(context()["target_character_id"]),tVid,tConf,tPlan,tManual);
        targetCard.SetAptitudeGrid(tGrid);
        targetCard.SetAffinityBadge(null);
        var chaseKeys=GridSpecs.Where(s=>chaseSToggles.TryGetValue(s.FactorKey,out var btn)&&btn.IsChecked==true).Select(s=>s.FactorKey).ToArray();
        if(chaseKeys.Length>0)
        {
            var parts=new List<string>();
            var badgeParts=new List<string>();
            foreach(var fk in chaseKeys)
            {
                var ps=pTot.GetValueOrDefault(fk,0);var gs=gTot.GetValueOrDefault(fk,0);var tot=ps+gs;
                var tier=tot>=12?"高星池":tot>=6?"中星池":tot>=1?"偏薄(<6★)":"未配0★";
                parts.Add($"{ShortFactorTag(fk)}S：{tot}★（亲{ps}+祖{gs}·{tier}）");
                badgeParts.Add($"{ShortFactorTag(fk)}{tot}★");
            }
            chaseSSummaryText.Text="已选追 S（待核对预览）："+string.Join(" | ",parts);
            targetCard.SetSubBadge("追S "+string.Join("·",badgeParts.Take(2))+(badgeParts.Count>2?$" +{badgeParts.Count-2}":""));
        }
        else
        {
            chaseSSummaryText.Text="局中追 S 目标（不占开局 6 槽名额；靠双亲+祖辈叠加红星在继承事件触发 S）。";
            targetCard.SetSubBadge(FormatUpgradeSubBadge(tGrid));
        }
    }

    private void BuildTenGridUi()
    {
        var header=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=8};
        var title=Note("十格适性表（可设开局目标，或设为 🔒 禁升 A 防污染）");title.FontWeight=FontWeight.SemiBold;
        var actions=new WrapPanel{HorizontalAlignment=HorizontalAlignment.Right};
        lockNonGoals.Margin=new Thickness(0,0,6,0);
        actions.Children.Add(lockNonGoals);
        actions.Children.Add(clearGoals);
        header.Children.Add(title);Grid.SetColumn(actions,1);header.Children.Add(actions);
        tenGridSection.Children.Add(header);
        AddGroupRow("场地适性",0,2);
        AddGroupRow("距离适性",2,4);
        AddGroupRow("跑法适性",6,4);
        tenGridSection.Children.Add(tenGridBudget);
    }

    private void AddGroupRow(string heading,int start,int count)
    {
        var label=new TextBlock{Text=heading,FontSize=11,Margin=new Thickness(0,2,0,0)};
        label.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
        tenGridSection.Children.Add(label);
        var grid=new Grid{ColumnDefinitions=new("*,*"),ColumnSpacing=6,RowSpacing=6};
        var rows=(count+1)/2;
        grid.RowDefinitions=new(string.Join(",",Enumerable.Repeat("Auto",rows)));
        for(var i=0;i<count;i++)
        {
            var cell=cells[start+i];
            Grid.SetRow(cell,i/2);Grid.SetColumn(cell,i%2);
            grid.Children.Add(cell);
        }
        tenGridSection.Children.Add(grid);
    }

    private void LockNonGoalAptitudes()
    {
        if(loading)return;
        if(cells.All(c=>c.EffectiveBaseline==null))
        {
            status.Text="请先在上方选择育成目标（必要时指定衣装），再一键锁定先天 B～E 的非目标适性。";
            return;
        }
        var lockedCount=0;
        var was=loading;loading=true;
        try
        {
            foreach(var cell in cells)
            {
                if(!cell.IsGoalActive&&cell.EffectiveBaseline is "B" or "C" or "D" or "E")
                {
                    cell.SetAvoidA();
                    lockedCount++;
                }
            }
        }
        finally{loading=was;}
        Changed();
        if(lockedCount==0)
            status.Text="当前没有未设目标且先天为 B～E 的适性可锁（F/G 天然不升 A，A 已是先天 A）。";
        else
            status.Text=$"已锁定 {lockedCount} 项先天 B～E 非目标适性为 🔒 禁升 A，防止稀释红因子池。";
    }

    private void ClearAllGoals()
    {
        if(loading)return;
        var was=loading;loading=true;
        try{foreach(var cell in cells)cell.ClearGoal();}
        finally{loading=was;}
        Changed();
    }

    private (string? Rank,bool Ambiguous,bool Known) LookupBaseline(string profileKey)
    {
        var id=Number(context()["target_character_id"]);
        if(id==null)return (null,false,false);
        var vid=(targetVariant.SelectedItem as Choice)?.Id??"";
        var matches=aptitudeProfiles.Where(r=>Number(r["character_id"])==id&&(vid.Length==0||Str(r["variant_id"])==vid)).ToArray();
        if(matches.Length==0)return (null,false,false);
        var distinct=matches.Select(r=>Str((r["aptitudes"] as JObject)?[profileKey])).Where(r=>Array.IndexOf(StartRanks,r)>=0).Distinct().ToArray();
        if(distinct.Length==1)return (distinct[0],false,true);
        return distinct.Length>1?(null,true,false):(null,false,false);
    }

    private void RefreshTenGridBaselines(bool clearExceeded)
    {
        var was=loading;loading=true;
        try{foreach(var cell in cells)cell.SyncBaseline(clearExceeded);}
        finally{loading=was;}
        RefreshBudgetSummary();
    }

    private void RefreshBudgetSummary()
    {
        var active=cells.Where(c=>c.IsGoalActive).ToArray();
        var locked=cells.Where(c=>c.IsAvoidA).ToArray();
        clearGoals.IsEnabled=active.Length>0||locked.Length>0;
        var lines=new List<string>();
        if(active.Length==0)
        {
            lines.Add(locked.Length>0?"当前不改开局适性。":"当前不改开局适性（保持先天等级开局）。");
        }
        else
        {
            var pills=active.Select(c=>c.ShortTag()).ToArray();
            var allKnown=active.All(c=>c.RequiredStars!=null);
            var totalStars=allKnown?active.Sum(c=>c.RequiredStars!.Value):(int?)null;
            var totalSlots=active.All(c=>c.MinSlots!=null)?active.Sum(c=>c.MinSlots!.Value):(int?)null;
            lines.Add("已选 "+active.Length+" 项："+string.Join(" · ",pills));
            if(totalStars is int stars&&totalSlots is int minSlots)
                lines.Add($"预计合计至少 {stars}★（理论 ≥{minSlots}/6 槽）"+(minSlots>6?" ⚠ 超出 6 槽上限，请减少目标！":""));
            else if(active.Any(c=>c.IsS))
                lines.Add("⚠ 包含 S 目标：开局仅红因子不可达，请在上方『局中追 S 目标』勾选并堆高红星池。");
            else if(active.Any(c=>c.ExceedsFourRaises))
                lines.Add("⚠ 有目标超出开局 +4 阶提升上限。");
            else
                lines.Add("部分先天等级待核对；选择衣装或手填后可估算红星与槽位。");
        }
        if(locked.Length>0)
        {
            lines.Add($"🔒 防污染锁 {locked.Length} 项：{string.Join(" · ",locked.Select(c=>c.ShortLockTag()))}");
            if(locked.Any(c=>c.EffectiveBaseline=="A"))
                lines.Add("⚠ 含先天已为 A 的防污染项，请检查衣装或改回“不改”。");
        }
        tenGridBudget.Text=string.Join("\n",lines);
    }

    private string[] ActiveGoalKeys()=>cells.Where(c=>c.IsActive).Select(c=>c.FactorKey).ToArray();
    private string DefaultRedFactorKey()=>cells.FirstOrDefault(c=>c.IsActive)?.FactorKey??"red:泥地";

    private static TextBlock Note(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=12};
    private static Control Field(string title,Control body)=>new StackPanel{Spacing=4,Margin=new Thickness(0,0,10,6),Children={Note(title),body}};
    private static string Str(JToken? value)=>value?.Type is JTokenType.String or JTokenType.Integer?value.ToString():"";
    private static int? Number(JToken? value)=>value?.Type==JTokenType.Integer?(int?)value:null;
    private static bool Bool(JToken? value)=>(bool?)value==true;
    private static string GradeSymbol(string? grade)=>(grade??"").Trim() switch{"double_circle" or "◎"=>"◎","good" or "circle" or "◯" or "〇"=>"◯","triangle" or "△"=>"△",""=>"-",_=>grade!};
    private static string SurfaceCn(string? surface)=>(surface??"").Trim().ToLowerInvariant() switch{"turf"=>"草地","dirt"=>"泥地",_=>surface??""};
    private static string DistanceCn(string? dist)=>(dist??"").Trim().ToLowerInvariant() switch{"sprint" or "short"=>"短距离","mile"=>"英里","medium" or "middle"=>"中距离","long"=>"长距离",_=>dist??""};
    private static string RunningStyleCn(string? style)=>(style??"").Trim().ToLowerInvariant() switch{"leader"=>"领放(逃)","front"=>"前列(先)","middle"=>"居中(差)","back"=>"后追(追)","keep"=>"维持默认",_=>string.IsNullOrWhiteSpace(style)?"维持默认":style!};
    private static string SlotShortName(int slotIndex)=>slotIndex>=0&&slotIndex<Titles.Length?Titles[slotIndex]:$"槽位{slotIndex+1}";
    private static async Task CopySchedulePresetAsync(Control anchor,TextBlock statusTarget,string slotTitle,JObject pacerPreset,int raceCount)
    {
        var clipboard=TopLevel.GetTopLevel(anchor)?.Clipboard;
        if(clipboard==null){statusTarget.Text="当前窗口剪贴板不可用，请改用导出赛程 JSON。";return;}
        try
        {
            await clipboard.SetTextAsync(pacerPreset.ToString(Formatting.Indented));
            statusTarget.Text=$"已复制「{slotTitle}」Pacer 参赛预设（{raceCount} 场无冲突 G1 · 零三连战）；可到 Pacer 自定义赛程日历一键粘贴导入。";
        }
        catch{statusTarget.Text="复制 Pacer 预设失败，请尝试导出赛程 JSON 文件。";}
    }
    private static async Task ExportSchedulePresetAsync(Control anchor,TextBlock statusTarget,string slotKey,JObject pacerPreset)
    {
        var owner=TopLevel.GetTopLevel(anchor) as Window;
        if(owner?.StorageProvider==null){statusTarget.Text="当前窗口暂不支持文件保存框，请尝试复制到剪贴板。";return;}
        try
        {
            var safeKey=string.Concat(slotKey.Select(c=>Path.GetInvalidFileNameChars().Contains(c)?'_':c));
            if(safeKey.Length==0)safeKey="slot";
            var file=await owner.StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
            {
                Title="导出 Pacer 历战赛程预设 JSON",
                SuggestedFileName=$"pacer_g1_schedule_{safeKey}.json",
                DefaultExtension="json",
                FileTypeChoices=new[]{new FilePickerFileType("JSON 赛程预设"){Patterns=new[]{"*.json"}}}
            });
            if(file==null)return;
            await using var stream=await file.OpenWriteAsync();stream.SetLength(0);
            await using var writer=new StreamWriter(stream,new UTF8Encoding(false));
            await writer.WriteAsync(pacerPreset.ToString(Formatting.Indented));
            statusTarget.Text=$"已导出 Pacer 历战赛程预设：{file.Name}";
        }
        catch{statusTarget.Text="导出赛程 JSON 未完成，请检查目录权限后重试。";}
    }
    private static string RaceSummaryLabel(JObject? value)
    {
        if(value==null)return "未录";var known=Number(value["known_win_count"]);var unverified=Number(value["unverified_count"])??0;
        if((bool?)value["complete"]==true)return known is int count?$"已确认 {count} 场（完整）":"记录完整";
        if(Str(value["coverage"])=="unknown"&&known==null)return "未录 / 未确认";
        return (known is int n?$"已确认 {n} 场":"已知胜场未确认")+(unverified>0?$"，另有 {unverified} 场待核对（未确认完整）":"（清单未齐）");
    }
    private static string CharacterLabel(JObject row)=>Str(row["name"]) is string name&&name.Length>0?name:"角色 #"+Str(row["character_id"]);
    private void Changed(){if(loading)return;generation++;bindings.Clear();foreach(var slot in slots)slot.ClearEvidence();graph?.SetAffinityHeader(null,null);aptitude.Text="方案已修改，适性与胜场尚未重新核对。";ready.Text="";results.Children.Clear();cachedDraft=null;ApplyLocks();RefreshBudgetSummary();RefreshTargetCard();RefreshVisualCards();aiPanel?.ContextChanged();externalPanel?.ContextChanged();autoPairPanel?.ContextChanged();DraftChanged?.Invoke();}
    private JObject AiContext(){var ctx=context();return new JObject{["target_character_id"]=ctx["target_character_id"]?.DeepClone(),["library_id"]=ctx["library_id"]?.DeepClone(),["account_id"]=ctx["account_id"]?.DeepClone(),["include_unassigned"]=(bool?)ctx["include_unassigned"]==true,["local_route"]=ReadDraft()};}
    private JObject ExternalContext()
    {
        var snapshot=AiContext();
        if(externalGoals?.Invoke() is JObject goals)
            foreach(var key in new[]{"targets","raw_targets"})
                if(goals[key] is JArray values)snapshot[key]=values.DeepClone();
        return snapshot;
    }
    public void SetTargetEditor(Control editor){if(targetCharacterEditor.Content is Control prior)targetCharacterEditor.Content=null;targetCharacterEditor.Content=editor;editor.IsVisible=true;}
    public void ShowTargetEditor(){ShowInspector(targetInspector,"育成目标与十格适性");targetCard.Select(true);foreach(var slot in slots)slot.Select(false);}
    private void ShowNode(int index){ShowInspector(slots[index].EditorView,Titles[index]);targetCard.Select(false);for(var i=0;i<slots.Length;i++)slots[i].Select(i==index);}
    private void ShowInspector(Control editor,string title){if(inspectorContent.Content is Control previous){inspectorContent.Content=null;previous.IsVisible=false;editorPool.Children.Add(previous);}editorPool.Children.Remove(editor);editor.IsVisible=true;inspectorContent.Content=editor;inspectorTitle.Text=title;inspector.IsVisible=true;LayoutInspector();if(Bounds.Width<(memberMode?1240:1380))Dispatcher.UIThread.Post(()=>{if(inspector.IsVisible)inspector.BringIntoView();});}
    public void CloseInspector()=>HideInspector();
    private void HideInspector(){if(inspectorContent.Content is Control previous){inspectorContent.Content=null;previous.IsVisible=false;editorPool.Children.Add(previous);}inspector.IsVisible=false;targetCard.Select(false);foreach(var slot in slots)slot.Select(false);LayoutInspector();Dispatcher.UIThread.Post(()=>graph.BringIntoView());}
    private bool? lastInspectorSide;
    private void LayoutInspector(){var side=inspector.IsVisible&&PacerLayoutGuard.NextNarrow(Bounds.Width,memberMode?1240:1380,lastInspectorSide.HasValue?!lastInspectorSide.Value:null,24)==false;if(lastInspectorSide==side&&workspace.ColumnDefinitions.Count>0)return;lastInspectorSide=side;workspace.ColumnDefinitions=new(side?(memberMode?"*,380":"*,420"):"*");workspace.RowDefinitions=new(side?"Auto":"Auto,Auto");Grid.SetColumn(graph,0);Grid.SetRow(graph,0);Grid.SetColumn(inspector,side?1:0);Grid.SetRow(inspector,side?0:1);}
    private void RefreshTargetCard()
    {
        var row=characters.FirstOrDefault(x=>Number(x["character_id"])==Number(context()["target_character_id"]));
        var variant=(targetVariant.SelectedItem as Choice)?.Id??"";
        targetCard.SetIdentity(row==null?"点击选择育成目标":CharacterLabel(row),OutfitName(row,variant),Portrait(row,variant));
        targetCard.SetState("本次育成目标");
        var active=cells.Where(c=>c.IsGoalActive).ToArray();
        var locked=cells.Where(c=>c.IsAvoidA).ToArray();
        string badge;
        if(active.Length==0)
            badge=locked.Length>0?$"🔒防污染{locked.Length}项":"不改适性";
        else
        {
            badge=active.Length<=2?string.Join(" · ",active.Select(c=>c.ShortTag())):string.Join(" · ",active.Take(2).Select(c=>c.ShortTag()))+$" +{active.Length-2}项";
            if(locked.Length>0)badge+=$" · 🔒防污染{locked.Length}项";
        }
        targetCard.SetRed(null,true,planned:badge);
    }
    private static string OutfitName(JObject? character,string variant){if(variant.Length==0)return "衣装未指定";var outfit=(character?["outfits"] as JArray??new()).OfType<JObject>().FirstOrDefault(x=>Str(x["variant_id"])==variant);var name=Str(outfit?["name"]);return name.Length>0?name:"衣装资料待核对";}
    private static string Portrait(JObject? character,string variant){var values=(character?["outfits"] as JArray??new()).OfType<JObject>();var id=variant.Length>0?variant:Str(values.FirstOrDefault()?["variant_id"]);if(id.Length!=6||!id.All(char.IsDigit))return "";var path=System.IO.Path.Combine(AppContext.BaseDirectory,"resource","portraits",id+".png");return System.IO.File.Exists(path)?path:"";}
    private void ApplyLocks(){foreach(var side in new[]{0,3}){var locked=slots[side].IsFinished;slots[side].SetLocked(false);slots[side+1].SetLocked(locked);slots[side+2].SetLocked(locked);}}
    public void SetEntries(JObject[] values)
    {
        var entriesChanged=!JToken.DeepEquals(new JArray(entries),new JArray(values));entries=values;foreach(var slot in slots)slot.RefreshEntries();var next=context();scope.Text="当前规划："+Str(next["account_label"])+"；已有 / 可借状态由账号资料核对。";
        var key=next.ToString();if(key!=priorContext||entriesChanged){priorContext=key;UpdateVariants();RefreshTenGridBaselines(clearExceeded:false);Changed();}
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
            characters=(data["characters"] as JArray??new()).OfType<JObject>().ToArray();
            aptitudeProfiles=(data["aptitude_profiles"] as JArray??new()).OfType<JObject>().ToArray();
            catalogueLibraryId=Str(data["library_id"]);
            var values=(data["aptitude_factors"] as JArray??new()).OfType<JObject>().Select(x=>new Choice(Str(x["factor_key"]),Str(x["label"]),x)).ToArray();
            redFactorChoices=values.Length>0?values:GridSpecs.Select(x=>new Choice(x.FactorKey,x.Label)).ToArray();
            foreach(var slot in slots)slot.RefreshCharacters();
            UpdateVariants();RefreshTenGridBaselines(clearExceeded:false);
            catalogLoaded=true;loading=false;if(cachedDraft!=null)LoadDraft(cachedDraft);RefreshTargetCard();RefreshVisualCards();autoPairPanel?.RefreshLockLabel();
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="本地路线目录暂不可用；保留草稿，请稍后刷新。";}finally{loading=false;}
    }
    public JObject ReadDraft()
    {
        if(!catalogLoaded&&cachedDraft!=null)return (JObject)cachedDraft.DeepClone();
        var rows=new JArray();for(var i=0;i<6;i++)rows.Add(i%3!=0&&slots[i/3*3].IsFinished?JValue.CreateNull():slots[i].Read()??JValue.CreateNull());
        var vid=StrId(targetVariant);
        var goals=new JArray();
        var avoids=new JArray();
        foreach(var cell in cells)
        {
            if(cell.ReadGoal() is JObject g)goals.Add(g);
            else if(cell.ReadAvoidA() is JObject a)avoids.Add(a);
        }
        var primary=goals.OfType<JObject>().FirstOrDefault();
        var legacyGoal=primary!=null
            ?new JObject{["factor_key"]=primary["factor_key"]!.DeepClone(),["baseline_rank"]=primary["baseline_rank"]?.DeepClone()??JValue.CreateNull(),["target_rank"]=primary["target_rank"]!.DeepClone(),["target_variant_id"]=vid.DeepClone()}
            :new JObject{["factor_key"]="red:泥地",["baseline_rank"]=JValue.CreateNull(),["target_rank"]="G",["target_variant_id"]=vid.DeepClone()};
        var chaseS=new JArray(GridSpecs.Where(s=>chaseSToggles.TryGetValue(s.FactorKey,out var btn)&&btn.IsChecked==true).Select(s=>s.FactorKey));
        var budgets=new JObject();
        foreach(var gi in new[]{1,2,4,5})
        {
            if(slots[gi/3*3].IsFinished)continue;
            if(slots[gi].ReadAncestorBudget() is JObject b)budgets[Keys[gi]]=b;
        }
        var draftObj=new JObject{["target_variant_id"]=vid,["aptitude_goals"]=goals,["avoid_a_factors"]=avoids,["aptitude_goal"]=legacyGoal,["nodes"]=rows};
        if(chaseS.Count>0)draftObj["chase_s_factors"]=chaseS;
        if(budgets.HasValues)draftObj["ancestor_red_budgets"]=budgets;
        return draftObj;
    }
    private static JToken StrId(ComboBox box)=>box.SelectedItem is Choice c&&c.Id.Length>0?new JValue(c.Id):JValue.CreateNull();
    public void LoadDraft(JObject? value)
    {
        var draft=value==null
            ?new JObject{["target_variant_id"]=null,["aptitude_goals"]=new JArray(),["avoid_a_factors"]=new JArray(),["aptitude_goal"]=new JObject{["factor_key"]="red:泥地",["baseline_rank"]=null,["target_rank"]="G",["target_variant_id"]=null},["nodes"]=new JArray(Enumerable.Repeat<JToken>(JValue.CreateNull(),6))}
            :(JObject)value.DeepClone();
        cachedDraft=draft;
        loading=true;
        try
        {
            var goal=draft["aptitude_goal"] as JObject??new();
            var vid=Str(draft["target_variant_id"]);
            if(vid.Length==0)vid=Str(goal["target_variant_id"]);
            UpdateVariants();
            var variants=targetVariant.ItemsSource as IEnumerable<Choice>??[];
            if(vid.Length>0&&variants.All(v=>v.Id!=vid)){var retained=new Choice(vid,"原衣装 #"+vid+"（资料待核对）");variants=variants.Append(retained).ToArray();targetVariant.ItemsSource=variants;}
            targetVariant.SelectedItem=variants.FirstOrDefault(x=>x.Id==vid)??variants.FirstOrDefault();
            foreach(var cell in cells)cell.SyncBaseline(clearExceeded:false);

            var goalMap=new Dictionary<string,JObject>(StringComparer.Ordinal);
            if(draft["aptitude_goals"] is JArray goalArray)
            {
                foreach(var row in goalArray.OfType<JObject>())
                {
                    var fk=Str(row["factor_key"]);
                    if(fk.StartsWith("pink:",StringComparison.Ordinal))fk="red:"+fk[5..];
                    if(fk.Length>0)goalMap[fk]=row;
                }
            }
            else if(draft["aptitude_goal"] is JObject single)
            {
                var fk=Str(single["factor_key"]);
                if(fk.StartsWith("pink:",StringComparison.Ordinal))fk="red:"+fk[5..];
                if(fk.Length>0)goalMap[fk]=single;
            }
            var avoidMap=new Dictionary<string,JObject>(StringComparer.Ordinal);
            if(draft["avoid_a_factors"] is JArray avoidArray)
            {
                foreach(var row in avoidArray.OfType<JObject>())
                {
                    var fk=Str(row["factor_key"]);
                    if(fk.StartsWith("pink:",StringComparison.Ordinal))fk="red:"+fk[5..];
                    if(fk.Length>0&&!goalMap.ContainsKey(fk))avoidMap[fk]=row;
                }
            }
            foreach(var cell in cells)
            {
                if(goalMap.TryGetValue(cell.FactorKey,out var g))
                    cell.LoadGoal(Str(g["baseline_rank"]),Str(g["target_rank"]));
                else if(avoidMap.TryGetValue(cell.FactorKey,out var a))
                    cell.LoadAvoidA(Str(a["baseline_rank"]));
                else
                    cell.ClearGoal();
            }
            var chaseSet=new HashSet<string>((draft["chase_s_factors"] as JArray??new()).Select(Str).Where(s=>s.Length>0),StringComparer.Ordinal);
            foreach(var kv in chaseSToggles)
                kv.Value.IsChecked=chaseSet.Contains(kv.Key)||(kv.Key.StartsWith("red:",StringComparison.Ordinal)&&chaseSet.Contains("pink:"+kv.Key[4..]));
            var nodes=draft["nodes"] as JArray??new();
            var budgetsObj=draft["ancestor_red_budgets"] as JObject;
            for(var i=0;i<6;i++){slots[i].ReferenceCharacter=null;slots[i].Load(nodes.ElementAtOrDefault(i) as JObject);slots[i].LoadBudget(budgetsObj?[Keys[i]] as JObject);}
        }
        finally{loading=false;}
        generation++;bindings.Clear();foreach(var slot in slots)slot.ClearEvidence();graph?.SetAffinityHeader(null,null);ApplyLocks();RefreshBudgetSummary();RefreshTargetCard();RefreshVisualCards();aiPanel.ContextChanged();externalPanel.ContextChanged();autoPairPanel?.ContextChanged();autoPairPanel?.RefreshLockLabel();results.Children.Clear();aptitude.Text="已载入本地路线草稿，尚未核对。";ready.Text="";
    }
    public void ApplyDraft(JObject value){LoadDraft(value);DraftChanged?.Invoke();}
    private static JObject CloneWithNodes(JObject currentDraft,JArray nodes)
    {
        var copy=(JObject)currentDraft.DeepClone();
        copy["nodes"]=nodes;
        return copy;
    }
    private void ImportFamily(){var ids=familyParents();LoadDraft(CloneWithNodes(ReadDraft(),new JArray(Enumerable.Range(0,6).Select(i=>i==0||i==3?ids.ElementAtOrDefault(i/3) is string id?(JToken)new JObject{["kind"]="entry",["entry_id"]=id}:JValue.CreateNull():JValue.CreateNull()))));status.Text="已带入当前两匹成品；祖辈核对时由真实记录展开，不复制手工改造系谱。";}
    public void ImportSavedFamily(string?[] parentIds,int?[] lineage,bool manualReference)
    {
        if(!manualReference){LoadDraft(CloneWithNodes(ReadDraft(),new JArray(Enumerable.Range(0,6).Select(i=>(i==0||i==3)&&parentIds.ElementAtOrDefault(i/3) is string id?(JToken)new JObject{["kind"]="entry",["entry_id"]=id}:JValue.CreateNull()))));}
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
        targetCard.SetAptitudeGrid(value["target_aptitude_grid"] as JArray);
        targetCard.SetAffinityBadge(null);
        if(value["affinity_summary"] is JObject aff)
        {
            var tot=Number(aff["total_with_race"])??Number(aff["total_base"]);
            var knownSub=Number(aff["known_subtotal"]);
            var sym=GradeSymbol(Str(aff["grade"]));
            var totalText=tot is int t?$"总相性 {t}（{sym}）":knownSub is int k?$"已知相性下限 {k}":null;
            var pm=Number(aff["parents_mutual_total"]);
            var mutualText=pm is int p?$"父母互通 +{p}":(Number(aff["parents_mutual_race_bonus"]) is int pr&&pr>0?$"父母互胜 +{pr}":null);
            graph.SetAffinityHeader(totalText,mutualText);
        }
        else
        {
            graph.SetAffinityHeader(null,null);
        }
        var chaseSummaryArr=(value["chase_s_summary"] as JArray??new()).OfType<JObject>().ToArray();
        if(chaseSummaryArr.Length>0)
        {
            var chaseLines=chaseSummaryArr.Select(x=>
            {
                var pTot=(Number(x["parent_confirmed"])??0)+(Number(x["parent_planned"])??0);
                var gTot=(Number(x["gp_confirmed"])??0)+(Number(x["gp_planned"])??0);
                return $"{Str(x["label"])}S：共 {Number(x["total_stars"])??0}★（亲{pTot}+祖{gTot} · {Str(x["tier_label"])}）";
            }).ToArray();
            chaseSSummaryText.Text="局中追 S 星池（已核对）："+string.Join(" | ",chaseLines);
            var badges=chaseSummaryArr.Select(x=>Str(x["short_label"])+(Number(x["total_stars"])??0)+"★").ToArray();
            targetCard.SetSubBadge("追S "+string.Join("·",badges.Take(2))+(badges.Length>2?$" +{badges.Length-2}":""));
        }
        else
        {
            chaseSSummaryText.Text="局中追 S 目标（不占开局 6 槽名额；靠双亲+祖辈叠加红星在继承事件触发 S）。";
            targetCard.SetSubBadge(FormatUpgradeSubBadge(value["target_aptitude_grid"] as JArray));
        }
        var aptitudeResult=value["aptitude"] as JObject??new();
        var items=aptitudeResult["items"] as JArray;
        var aptLines=new List<string>();
        if(items!=null)
        {
            if(items.Count==0)
            {
                aptLines.Add("当前未设置开局改适性目标（不额外要求红因子星数）。");
            }
            else
            {
                var aggState=Str(aptitudeResult["status"]);
                var totKnown=Number(aptitudeResult["total_known_goal_stars"]);
                var totReq=Number(aptitudeResult["total_required_stars"]);
                var header=aggState switch
                {
                    "unreachable_at_start"=>"开局不可达：部分目标超出开局适性上限（仅 A 及以下可由开局红因子达成）。",
                    "unknown_baseline"=>"部分目标先天适性未知：请指定准确衣装或在十格表中手动填写。",
                    "slot_capacity_exceeded"=>"超出 6 槽物理容量：6 个继承位无法同时塞下全部红因子星数。",
                    "satisfied"=>$"多维适性已满足（{items.Count} 项） · 已确认目标红星 {totKnown?.ToString()??"0"}/{totReq?.ToString()??"0"}",
                    "missing"=>$"多维适性未齐（已满足 {Number(aptitudeResult["satisfied_count"])??0}/{items.Count} 项） · 仍缺 {Number(aptitudeResult["total_gap_from_confirmed"])?.ToString()??"?"} 星",
                    _=>$"多维适性待确认（已满足 {Number(aptitudeResult["satisfied_count"])??0}/{items.Count} 项）"
                };
                aptLines.Add(header);
                foreach(var it in items.OfType<JObject>())
                {
                    var b=it["baseline"] as JObject??new();var p=it["plan"] as JObject??new();
                    var bRank=Str(b["rank"]);if(bRank.Length==0)bRank=Str(b["status"])=="ambiguous_variant"?"多衣装冲突":"未知";
                    var known=Number(p["known_stars"]);var req=Number(p["required_stars"]);var gap=Number(p["gap_from_confirmed"]);
                    var unk=Number(p["unknown_nodes"])??0;
                    var itemState=Str(p["status"]) switch
                    {
                        "satisfied"=>"已满足",
                        "unreachable_at_start"=>"开局不可达（最高 "+Str(p["max_start_rank"])+"）",
                        "unknown_baseline"=>"基准未知",
                        "missing"=>$"差 {gap?.ToString()??"?"}★",
                        _=>$"差 {gap?.ToString()??"?"}★（未知槽 {unk}）"
                    };
                    aptLines.Add($"• {Str(it["label"])} {bRank}→{Str(it["target_rank"])}：{(known?.ToString()??"未知")}/{(req?.ToString()??"不可达")}★ · {itemState}");
                }
                if(aptitudeResult["slot_budget"] is JObject sb)
                {
                    aptLines.Add($"6槽物理占用：目标 {Number(sb["occupied_goal_slots"])??0} · 非目标 {Number(sb["occupied_non_goal_slots"])??0} · 可调 {Number(sb["open_slots"])??0}（缺口至少需 {Number(sb["minimum_additional_slots_needed"])??0} 槽）");
                }
            }
        }
        else
        {
            var plan=aptitudeResult["plan"] as JObject??new();var baselineResult=aptitudeResult["baseline"] as JObject??new();var state=Str(plan["status"]);var known=Number(plan["known_stars"]);var gap=Number(plan["additional_stars"]);var lowerGap=Number(plan["gap_from_confirmed"]);var unknown=Number(plan["unknown_nodes"])??(plan["unknown_nodes"] as JArray)?.Count??0;
            var singleLine=state switch{"unreachable_at_start"=>"开局不可达：当前目标超出开局适性上限，不显示普通缺星。","unknown_baseline"=>"基准适性未知：请指定准确衣装或手动基准等级。","satisfied"=>$"适性要求已满足 · 已确认红星 {known?.ToString()??"未知"}",_=>$"已确认红星 {known?.ToString()??"未知"} · "+(gap is int n?$"仍缺 {n} 星":"缺星尚不能确认")+(unknown>0?$" · 未确认节点 {unknown}":"")};
            if(unknown>0&&state is "satisfied" or "unknown_baseline" or "unreachable_at_start")singleLine+=$" · 未确认节点 {unknown}";
            aptLines.Add(singleLine);
            if(Str(baselineResult["status"])=="ambiguous_variant")aptLines.Add("同角色不同衣装的基准不一致，请选择准确衣装或手动填写。");
            else if(Str(baselineResult["rank"]).Length>0)aptLines.Add("基准："+(Str(baselineResult["status"])=="manual"?"手动提供 ":"资料参考 ")+Str(baselineResult["rank"]));
            if(gap==null&&lowerGap is int lower&&state!="unreachable_at_start"&&state!="unknown_baseline")aptLines.Add($"已确认部分距目标还差 {lower} 星；未知节点补齐前，不能当作最终缺星。");
        }
        AppendAntiALines(aptLines,aptitudeResult["anti_a"] as JObject);
        aptitude.Text=string.Join("\n",aptLines);
        ready.Text=(bool?)value["ready_to_start"]==true?"本地资料已满足当前路线检查；请在游戏开始前自行核对。":"当前为规划草稿，尚未满足开局资料要求。";var issues=(value["selection_issues"] as JArray??new()).Select(Str).Where(s=>s.Length>0).ToArray();if(issues.Length>0)ready.Text+="\n需要调整："+string.Join(" ",issues.Take(2));results.Children.Clear();
        foreach(var issue in issues)results.Children.Add(Note("需要调整："+issue));
        var comp=value["compatibility"] as JObject;
        var raceAffinity=(value["race_affinity"] as JObject)??(comp?["race_affinity"] as JObject);
        if(comp!=null||raceAffinity!=null)
        {
            var compCardBody=new StackPanel{Spacing=4};
            compCardBody.Children.Add(new TextBlock{Text="相性与共同重赏 G1 加分",FontWeight=FontWeight.SemiBold,FontSize=13});
            if(comp!=null&&raceAffinity!=null)
            {
                var baseScoreText=Number(comp["total"]) is int ct?ct.ToString():(Number(comp["known_subtotal"]) is int ks?$"已知 {ks}":"待定");
                var bonus=Number(raceAffinity["bonus_points"])??0;
                var totalWithBonus=Number(raceAffinity["total_with_race_bonus"])??((Number(comp["total"])??Number(comp["known_subtotal"])??0)+bonus);
                var gradeWithBonus=GradeSymbol(Str(raceAffinity["grade_with_race_bonus"]) is {Length:>0} gb?gb:Str(comp["grade"]));
                var sharedCount=Number(raceAffinity["confirmed_shared_g1_count"])??0;
                var completeness=Bool(raceAffinity["complete"])?"完整精确":"已知下限";
                compCardBody.Children.Add(Note($"总相性（含共同 G1）：{totalWithBonus}（{gradeWithBonus}） · 基础角色相性 {baseScoreText} + 共同 G1 胜鞍 +{bonus}（已确认 {sharedCount} 场 · {completeness}）"));
            }
            else if(comp!=null)
            {
                var baseScoreText=Number(comp["total"]) is int ct?$"{ct}（{GradeSymbol(Str(comp["grade"]))}）":$"已知 {Number(comp["known_subtotal"])??0}";
                compCardBody.Children.Add(Note($"基础角色相性：{baseScoreText}"));
            }
            else if(raceAffinity!=null)
            {
                var bonus=Number(raceAffinity["bonus_points"])??0;
                var sharedCount=Number(raceAffinity["confirmed_shared_g1_count"])??0;
                var completeness=Bool(raceAffinity["complete"])?"完整精确":"已知下限";
                compCardBody.Children.Add(Note($"共同 G1 胜鞍加分：+{bonus} 相性（已确认 {sharedCount} 场 · {completeness}）"));
            }
            results.Children.Add(new Border{Padding=new Thickness(9,7),CornerRadius=new CornerRadius(8),BorderThickness=new Thickness(1),BorderBrush=Brushes.Gray,Child=compCardBody});
        }
        foreach(var link in (value["race_links"] as JArray??new()).OfType<JObject>())
        {
            var linkBonusText=Number(link["bonus_points"]) is int linkBonus?$" · 相性 +{linkBonus}":"";
            results.Children.Add(Note(NodeTitle(Str(link["left"]))+" × "+NodeTitle(Str(link["right"]))+"：已知共同 G1 "+(Number(link["known_common_count"])?.ToString()??"未知")+((bool?)link["complete"]==true?"（记录完整）":"（记录未齐，仅为已知下限）")+linkBonusText));
        }
        foreach(var task in (value["tasks"] as JArray??new()).OfType<JObject>())
        {
            var target=task["red_target"] as JObject;var red=target==null?"":" · 计划目标 "+Str(target["factor_key"]).Replace("red:","")+" "+Str(target["stars"])+" 星（尚未获得）";
            results.Children.Add(Note("待培养："+Str(task["label"])+red+" · "+Str(task["reason"])));
            if(task["recommended_schedule"] is JObject sched)results.Children.Add(BuildRecommendedScheduleCard(task,sched));
        }
        foreach(var note in (value["notes"] as JArray??new()).Select(Str).Where(x=>x.Length>0))results.Children.Add(Note(note));
    }
    private Control BuildRecommendedScheduleCard(JObject task,JObject sched)
    {
        var rawNode=Str(task["node"]).Length>0?Str(task["node"]):Str(task["node_key"]);
        var slotIdx=Number(task["slot_index"])??Array.IndexOf(Keys,rawNode);
        var slotKey=rawNode.Length>0?rawNode:(slotIdx>=0&&slotIdx<Keys.Length?Keys[slotIdx]:"planned");
        var slotTitle=Str(task["label"]).Length>0?Str(task["label"]):SlotShortName(slotIdx);
        var raceCount=Number(sched["race_count"])??0;
        var projBonus=Number(sched["projected_shared_g1_bonus"])??0;
        var styleCn=RunningStyleCn(Str(sched["recommended_running_style"]));
        var bonusSuffix=projBonus>0?$" · 预计共同 G1 +{projBonus} 相性":"";
        var cardBody=new StackPanel{Spacing=4};
        cardBody.Children.Add(new TextBlock{Text=$"📅 推荐凯旋门历战赛程（{slotTitle}）：{raceCount} 场无冲突 G1 · 零三连战 · 推荐跑法：{styleCn}{bonusSuffix}",FontWeight=FontWeight.SemiBold,TextWrapping=TextWrapping.Wrap,FontSize=12});
        var surfaces=string.Join(" / ",(sched["runnable_surfaces"] as JArray??new()).Select(x=>SurfaceCn(Str(x))).Where(s=>s.Length>0));
        var dists=string.Join(" / ",(sched["runnable_distances"] as JArray??new()).Select(x=>DistanceCn(Str(x))).Where(s=>s.Length>0));
        var dirtLabel=Str(sched["dirt_farming_label"]);
        var dirtSuffix=dirtLabel.Length>0?$" · {dirtLabel}":"";
        cardBody.Children.Add(Note($"可跑适性（含开局继承）：场地 {(surfaces.Length>0?surfaces:"待核")} · 距离 {(dists.Length>0?dists:"待核")}{dirtSuffix}"));
        var races=(sched["races"] as JArray??new()).OfType<JObject>().ToArray();
        if(races.Length==0)cardBody.Children.Add(Note("当前条件下未匹配到可选凯旋门 G1 赛程。"));
        else
        {
            foreach(var r in races)
            {
                var turnNum=Number(r["global_turn"])??((Number(r["larc_ordinal"])??0)+1);
                var rName=Str(r["display_name"]);if(rName.Length==0)rName=Str(r["race_id"]);
                var sharedArr=r["shared_with"] as JArray;
                var sharedTag=sharedArr!=null&&sharedArr.Count>0?" ★共享G1":"";
                cardBody.Children.Add(Note($"• T{turnNum} {Str(r["date_label"])} {rName}（{SurfaceCn(Str(r["surface"]))}{Str(r["distance_m"])}m）{sharedTag}"));
            }
        }
        if(sched["pacer_preset"] is JObject presetObj)
        {
            var actions=new WrapPanel{Margin=new Thickness(0,2,0,0)};
            var copyBtn=new Button{Name=$"LocalRouteCopySchedule_{slotKey}",Content="📋 复制 Pacer 参赛预设",FontSize=12,Margin=new Thickness(0,0,8,0)};
            var exportBtn=new Button{Name=$"LocalRouteExportSchedule_{slotKey}",Content="💾 导出赛程 JSON",FontSize=12};
            copyBtn.Click+=async(_,_)=>await CopySchedulePresetAsync(this,status,slotTitle,presetObj,raceCount);
            exportBtn.Click+=async(_,_)=>await ExportSchedulePresetAsync(this,status,slotKey,presetObj);
            actions.Children.Add(copyBtn);actions.Children.Add(exportBtn);
            cardBody.Children.Add(actions);
        }
        return new Border{Padding=new Thickness(9,7),CornerRadius=new CornerRadius(8),BorderThickness=new Thickness(1),BorderBrush=Brushes.SteelBlue,Child=cardBody};
    }
    private static void AppendAntiALines(List<string> lines,JObject? antiA)
    {
        if(antiA==null)return;
        if(antiA["red_pool"] is JObject rp&&(bool?)rp["complete_profile"]==true)
        {
            var poolLine=$"开局 A 适性红池：先天 {Number(rp["baseline_a_count"])??0} 项 → 当前实锤 {Number(rp["confirmed_a_count"])??0} 项";
            var unintended=(rp["unintended_a_labels"] as JArray??new()).Select(Str).Where(x=>x.Length>0).ToArray();
            if(unintended.Length>0)poolLine+=$"（意外升 A：{string.Join("、",unintended)}）";
            lines.Add(poolLine);
        }
        if(antiA["items"] is not JArray antiItems||antiItems.Count==0)return;
        var antiHeader=Str(antiA["status"]) switch
        {
            "polluted"=>"⚠ 防污染锁告警：存在已触及升 A 阈值的红因子，开局会污染红池！",
            "planned_polluted"=>"⚠ 防污染锁告警：待培养计划红因子将触发升 A 污染！",
            "already_a_at_baseline"=>"⚠ 防污染锁提示：所选保护项中含先天已为 A 的适性。",
            "unknown_baseline"=>"防污染锁待核对：部分保护项先天等级未知。",
            "unconfirmed_risk"=>"防污染锁待确认：未知槽位满星下可能触发升 A。",
            _=>$"🔒 防污染锁安全（{antiItems.Count} 项均未触及升 A 阈值）"
        };
        lines.Add(antiHeader);
        foreach(var ai in antiItems.OfType<JObject>())
        {
            var b=ai["baseline"] as JObject??new();
            var bRank=Str(b["rank"]);if(bRank.Length==0)bRank=Str(b["status"])=="ambiguous_variant"?"多衣装冲突":"未知";
            var knownStars=Number(ai["known_stars"]);
            var plannedStars=Number(ai["planned_stars"])??0;
            var maxSafe=Number(ai["max_safe_stars"]);
            var thresh=Number(ai["a_threshold_stars"]);
            var unk=Number(ai["unknown_nodes"])??0;
            var rem=Number(ai["remaining_safe_stars"]);
            var stateText=Str(ai["status"]) switch
            {
                "polluted"=>thresh is int t?$"⚠ 已升 A 污染红池（实锤 ≥{t}★）！":"⚠ 已升 A 污染红池！",
                "planned_polluted"=>$"⚠ 计划叠加后 {(knownStars??0)+plannedStars}★ ≥{thresh?.ToString()??"?"}★ 将触发升 A 污染！",
                "already_a_at_baseline"=>"⚠ 先天已为 A",
                "unknown_baseline"=>"基准未知",
                "immune_at_start"=>"天然不升 A",
                "unconfirmed_risk"=>$"待确认（最多再容忍 {rem?.ToString()??"?"}★，未知槽 {unk}）",
                _=>maxSafe is int ms&&ms<18?$"安全（≤{ms}★）":"安全（天然不升 A）"
            };
            var planSuffix=plannedStars>0?$"（+计划 {plannedStars}★）":"";
            lines.Add($"  • 🔒 {Str(ai["label"])}（先天 {bRank}）：已确认 {knownStars?.ToString()??"未知"}★{planSuffix}"+(maxSafe is int cap&&cap>=0&&cap<18?$" / 安全上限 {cap}★":"")+$" · {stateText}");
        }
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

    private sealed class AptitudeCell : Border
    {
        public const string AvoidAId="avoid_a";
        private readonly ParentLocalRouteView owner;
        private readonly AptitudeSpec spec;
        private readonly TextBlock titleLabel=new(){FontSize=12,FontWeight=FontWeight.SemiBold};
        private readonly TextBlock baseLabel=new(){FontSize=11,HorizontalAlignment=HorizontalAlignment.Right};
        private readonly ComboBox targetBox=new(){HorizontalAlignment=HorizontalAlignment.Stretch,MinWidth=78};
        private readonly ComboBox manualBox=new(){ItemsSource=new[]{"自动参考","G","F","E","D","C","B","A"},SelectedIndex=0,IsVisible=false,HorizontalAlignment=HorizontalAlignment.Stretch,Margin=new Thickness(0,4,0,0)};
        private readonly TextBlock hintLabel=new(){FontSize=10,TextWrapping=TextWrapping.Wrap,IsVisible=false};
        private string? catalogRank;
        private bool applying,ambiguous,known;
        public string FactorKey=>spec.FactorKey;
        public bool IsAvoidA=>SelectedTargetRank==AvoidAId;
        public bool IsGoalActive=>SelectedTargetRank.Length>0&&!IsAvoidA;
        public bool IsActive=>IsGoalActive;
        public string SelectedTargetRank=>(targetBox.SelectedItem as Choice)?.Id??"";
        public string? ManualRank=>manualBox.SelectedIndex>0?(string?)manualBox.SelectedItem:null;
        public string? EffectiveBaseline=>ManualRank??catalogRank;
        public bool IsS=>SelectedTargetRank=="S";
        public int? MaxSafeStars=>EffectiveBaseline switch
        {
            "B"=>0,
            "C"=>3,
            "D"=>6,
            "E"=>9,
            "F" or "G"=>18,
            "A"=>-1,
            _=>null
        };
        public bool ExceedsFourRaises
        {
            get
            {
                if(!IsGoalActive||IsS||EffectiveBaseline is not string b)return false;
                var bi=Array.IndexOf(StartRanks,b);var ti=Array.IndexOf(StartRanks,SelectedTargetRank);
                return bi>=0&&ti>bi&&ti-bi>4;
            }
        }
        public int? RequiredStars
        {
            get
            {
                if(!IsGoalActive||IsS||EffectiveBaseline is not string b)return null;
                var bi=Array.IndexOf(StartRanks,b);var ti=Array.IndexOf(StartRanks,SelectedTargetRank);
                if(bi<0||ti<0)return null;
                if(ti<=bi)return 0;
                return ti-bi>4?null:StarThresholds[ti-bi];
            }
        }
        public int? MinSlots=>RequiredStars is int r?(r==0?0:(r+2)/3):null;

        public AptitudeCell(ParentLocalRouteView owner,AptitudeSpec spec)
        {
            this.owner=owner;this.spec=spec;
            Name="AptitudeCell_"+spec.ProfileKey;
            targetBox.Name="AptitudeTarget_"+spec.ProfileKey;
            manualBox.Name="AptitudeManualBase_"+spec.ProfileKey;
            Padding=new Thickness(7,6);CornerRadius=new CornerRadius(8);BorderThickness=new Thickness(1);
            this.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
            baseLabel.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
            hintLabel.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
            var top=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=4};
            titleLabel.Text=spec.Label;top.Children.Add(titleLabel);Grid.SetColumn(baseLabel,1);top.Children.Add(baseLabel);
            var body=new StackPanel{Spacing=3,Children={top,targetBox,manualBox,hintLabel}};
            Child=body;
            targetBox.SelectionChanged+=(_,_)=>{if(applying)return;UpdateVisual();owner.Changed();};
            manualBox.SelectionChanged+=(_,_)=>{if(applying)return;RebuildTargets(preserveCurrent:SelectedTargetRank,clearExceeded:true);UpdateVisual();owner.Changed();};
            RebuildTargets(preserveCurrent:"",clearExceeded:false);UpdateVisual();
        }

        public void SyncBaseline(bool clearExceeded)
        {
            var (rank,isAmbiguous,isKnown)=owner.LookupBaseline(spec.ProfileKey);
            catalogRank=rank;ambiguous=isAmbiguous;known=isKnown;
            RebuildTargets(preserveCurrent:SelectedTargetRank,clearExceeded:clearExceeded);
            UpdateVisual();
        }

        public void ClearGoal()
        {
            var was=applying;applying=true;
            try{manualBox.SelectedIndex=0;RebuildTargets(preserveCurrent:"",clearExceeded:false);}
            finally{applying=was;}
            UpdateVisual();
        }

        public void SetAvoidA()
        {
            var was=applying;applying=true;
            try{RebuildTargets(preserveCurrent:AvoidAId,clearExceeded:false);}
            finally{applying=was;}
            UpdateVisual();
        }

        public void LoadAvoidA(string baselineRank)
        {
            var was=applying;applying=true;
            try
            {
                var idx=Array.IndexOf(["","G","F","E","D","C","B","A"],baselineRank);
                manualBox.SelectedIndex=idx>=0?idx:0;
                RebuildTargets(preserveCurrent:AvoidAId,clearExceeded:false);
            }
            finally{applying=was;}
            UpdateVisual();
        }

        public void LoadGoal(string baselineRank,string targetRank)
        {
            var was=applying;applying=true;
            try
            {
                var idx=Array.IndexOf(["","G","F","E","D","C","B","A"],baselineRank);
                manualBox.SelectedIndex=idx>=0?idx:0;
                RebuildTargets(preserveCurrent:targetRank,clearExceeded:false);
            }
            finally{applying=was;}
            UpdateVisual();
        }

        public JObject? ReadGoal()
        {
            if(!IsGoalActive)return null;
            return new JObject{["factor_key"]=spec.FactorKey,["baseline_rank"]=ManualRank is string m?new JValue(m):JValue.CreateNull(),["target_rank"]=SelectedTargetRank};
        }

        public JObject? ReadAvoidA()
        {
            if(!IsAvoidA)return null;
            return new JObject{["factor_key"]=spec.FactorKey,["baseline_rank"]=ManualRank is string m?new JValue(m):JValue.CreateNull()};
        }

        public string ShortTag()
        {
            var abbr=spec.Label.Length<=2?spec.Label:spec.Label[..1];
            var b=EffectiveBaseline??"?";
            var req=RequiredStars is int r?$"({r}★)":"";
            return $"{abbr}{b}→{SelectedTargetRank}{req}";
        }

        public string ShortLockTag()
        {
            var abbr=spec.Label.Length<=2?spec.Label:spec.Label[..1];
            var b=EffectiveBaseline??"?";
            return MaxSafeStars switch
            {
                >=0 and <18=>$"🔒{abbr}{b}(≤{MaxSafeStars}★)",
                18=>$"🔒{abbr}{b}(不升A)",
                -1=>$"⚠{abbr}已A",
                _=>$"🔒{abbr}{b}"
            };
        }

        private void RebuildTargets(string preserveCurrent,bool clearExceeded)
        {
            var was=applying;applying=true;
            try
            {
                var choices=new List<Choice>{new("","不改")};
                var baseRank=EffectiveBaseline;
                var baseIdx=baseRank!=null?Array.IndexOf(StartRanks,baseRank):-1;
                if(baseIdx>=0)
                {
                    for(var i=baseIdx+1;i<=Math.Min(6,baseIdx+4);i++)
                        choices.Add(new Choice(StartRanks[i],"→ "+StartRanks[i]));
                }
                else
                {
                    foreach(var rank in StartRanks)
                        choices.Add(new Choice(rank,"→ "+rank));
                }
                if(baseIdx!=6)choices.Add(new Choice(AvoidAId,"🔒 禁升 A"));
                var desired=preserveCurrent;
                if(clearExceeded&&desired.Length>0&&baseIdx>=0)
                {
                    if(desired==AvoidAId)
                    {
                        if(baseIdx==6)desired="";
                    }
                    else
                    {
                        var ti=Array.IndexOf(StartRanks,desired);
                        if(ti>=0&&ti<=baseIdx)desired="";
                    }
                }
                if(desired==AvoidAId&&choices.All(c=>c.Id!=AvoidAId))
                {
                    choices.Add(new Choice(AvoidAId,"🔒 禁升 A（先天已A·无效）"));
                }
                else if(desired.Length>0&&choices.All(c=>c.Id!=desired)&&Array.IndexOf(AllRanks,desired)>=0)
                {
                    choices.Add(new Choice(desired,desired=="S"?"S（仅局内继承·不可达）":"→ "+desired+"（待核）"));
                }
                targetBox.ItemsSource=choices;
                targetBox.SelectedItem=choices.FirstOrDefault(c=>c.Id==desired)??choices[0];
            }
            finally{applying=was;}
        }

        private void UpdateVisual()
        {
            var baseText=ManualRank is string m?$"手填 {m}":known&&catalogRank!=null?$"先天 {catalogRank}":ambiguous?"多衣装不同":"先天待核";
            baseLabel.Text=baseText;
            manualBox.IsVisible=(!known||ManualRank!=null)&&(IsGoalActive||IsAvoidA||ambiguous||ManualRank!=null);
            ToolTip.SetTip(targetBox,EffectiveBaseline=="A"?"先天已达 A（开局红因子最高改到 A；S 仅能局内继承）":"选择开局目标适性（最多 +4 阶且最高 A），或设为 🔒 禁升 A 保护红因子池");
            if(IsAvoidA)
            {
                BorderThickness=new Thickness(1.5);
                BorderBrush=new SolidColorBrush(Color.Parse("#D97706"));
                hintLabel.Text=EffectiveBaseline switch
                {
                    "B"=>"🔒 容忍 0★（沾 1★ 即升 A 污染！）",
                    "C"=>"🔒 最多容忍 3★（≥4★ 升 A）",
                    "D"=>"🔒 最多容忍 6★（≥7★ 升 A）",
                    "E"=>"🔒 最多容忍 9★（≥10★ 升 A）",
                    "F" or "G"=>"🔒 先天 F/G 开局最高 B/C，天然免疫",
                    "A"=>"⚠ 先天已为 A，无法防升 A",
                    _=>"🔒 请选准确衣装或手填先天"
                };
                hintLabel.IsVisible=true;
                return;
            }
            if(!IsGoalActive)
            {
                hintLabel.IsVisible=false;
                BorderThickness=new Thickness(1);
                this.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));
                return;
            }
            BorderThickness=new Thickness(1.5);
            this.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiPrimaryColor"));
            if(IsS)hintLabel.Text="开局仅红因子不可达（最高 A）";
            else if(EffectiveBaseline is string b)
            {
                var bi=Array.IndexOf(StartRanks,b);var ti=Array.IndexOf(StartRanks,SelectedTargetRank);
                if(bi>=0&&ti>bi)
                {
                    if(ti-bi>4)hintLabel.Text=$"超+4阶上限（最高 {StartRanks[Math.Min(6,bi+4)]}）";
                    else{var req=StarThresholds[ti-bi];hintLabel.Text=$"需 {req}★ · ≥{(req+2)/3} 槽";}
                }
                else hintLabel.Text="无需补星";
            }
            else hintLabel.Text="请选准确衣装或手填先天";
            hintLabel.IsVisible=true;
        }
    }

    private sealed class Slot : Border
    {
        private static readonly Choice[] StarChoices=[new("0","0★"),new("1","1★"),new("2","2★"),new("3","3★")];
        private readonly ParentLocalRouteView owner;private readonly int index;private bool applying,locked;
        private readonly ComboBox kind=new(){ItemsSource=new[]{"未选","库内成品","待培养角色","吗哩吗哩参考"},SelectedIndex=0};
        private readonly TextBox search=new(){Watermark="输入角色、衣装或记录编号筛选"};
        private readonly ComboBox selection=new(){HorizontalAlignment=HorizontalAlignment.Stretch};
        private readonly ComboBox variant=new(){HorizontalAlignment=HorizontalAlignment.Stretch};
        private readonly ComboBox redFactor=new(){HorizontalAlignment=HorizontalAlignment.Stretch};
        private readonly StackPanel redGoal=new(){Spacing=4};
        private readonly NumericUpDown stars=new(){Minimum=0,Maximum=3,Value=0,Increment=1,FormatString="0",Width=120};
        private readonly TextBlock state=Note("快照资料未齐"),detail=Note(""),ancestorReadOnlyNote=Note("");
        private readonly Button wins=new(){Content="编辑实际胜场",IsEnabled=false},rebreed=new(){Content="调整祖辈并规划重育",IsVisible=false};
        private readonly Button external=new(){Content="用吗哩吗哩补这一侧"};
        private readonly StackPanel editor=new(){Spacing=5};private JObject? retained;
        private readonly StackPanel ancestorBudgetBox=new(){Spacing=5,IsVisible=false};
        private readonly Expander l5Expander=new(){Header="第 5 层曾曾祖辈红星预算（抬本祖辈开局适性）",IsExpanded=false};
        private readonly ComboBox[] l4Factor=new ComboBox[2],l4Stars=new ComboBox[2];
        private readonly ComboBox[] l5Factor=new ComboBox[4],l5Stars=new ComboBox[4];
        private readonly Button clearBudgets=new(){Content="清空第4/5层预算",FontSize=11,Padding=new Thickness(8,3)};
        private readonly PedigreeNodeCard compact;
        private JObject? evidence;
        public StackPanel EditorView {get;}=new(){Spacing=7};
        public int? ReferenceCharacter {get;set;}
        public bool IsManualReference=>ReferenceCharacter!=null&&Kind==null&&!locked;
        public bool IsLockedCandidate=>!locked&&(Kind is "entry" or "external"||(Kind=="planned"&&CharacterId!=null));
        public string ShortLockLabel()
        {
            var name=Kind=="external"
                ?Str(ExternalIdentity?["character_name"]??ExternalFact?["character_name"])
                :Str((selection.SelectedItem as Choice)?.Row?["character_name"]??(selection.SelectedItem as Choice)?.Row?["name"]);
            if(name.Length==0&&CharacterId is int cid)
                name=Str(owner.characters.FirstOrDefault(c=>Number(c["character_id"])==cid)?["name"]);
            return name.Length>0?$"{Titles[index]} {name}":Titles[index];
        }
        public int? CharacterId=>Number(evidence?["character_id"])
            ??(Kind=="external"||ExternalBranch?ExternalCharacterId():Number((selection.SelectedItem as Choice)?.Row?["character_id"]))
            ??Number(retained?["character_id"])??ReferenceCharacter;
        public string SelectedVariantId=>Str(evidence?["variant_id"]) is {Length:>0} ev?ev
            :(IsFinished||ExternalBranch?Str((ExternalBranch?ExternalIdentity:(selection.SelectedItem as Choice)?.Row)?["variant_id"]):(variant.SelectedItem as Choice)?.Id??"");
        public string? Kind=>kind.SelectedIndex switch{1=>"entry",2=>"planned",3=>"external",_=>null};
        public bool IsFinished=>Kind=="entry"||ExternalEntry!=null;
        private JObject? ExternalEntry=>Kind=="external"?retained?["entry"] as JObject:null;
        private JObject? ExternalFact=>index%3==0||Kind=="external"?ExternalEntry?["self"] as JObject:
            locked?(owner.slots[index/3*3].ExternalEntry?["parents"] as JArray)?.ElementAtOrDefault(index%3-1) as JObject:null;
        private JObject? ExternalIdentity=>ExternalEntry??ExternalFact;
        private bool ExternalBranch=>Kind=="external"||locked&&index%3!=0&&owner.slots[index/3*3].ExternalEntry!=null;
        private int? ExternalCharacterId()=>int.TryParse(Str(ExternalIdentity?["character_id"]??ExternalFact?["character_id"]),out var id)?id:null;
        public Slot(ParentLocalRouteView owner,int index)
        {
            this.owner=owner;this.index=index;Name="LocalRouteSlot_"+Keys[index];compact=new PedigreeNodeCard(Keys[index],Titles[index]);compact.Click+=(_,_)=>owner.ShowNode(index);Child=compact;
            kind.Name="LocalRouteKind_"+Keys[index];search.Name="LocalRouteSearch_"+Keys[index];selection.Name="LocalRouteChoice_"+Keys[index];variant.Name="LocalRouteVariant_"+Keys[index];stars.Name="LocalRouteStars_"+Keys[index];redFactor.Name="LocalRouteRedFactor_"+Keys[index];wins.Name="LocalRouteWins_"+Keys[index];rebreed.Name="LocalRouteRebreed_"+Keys[index];state.Name="LocalRouteState_"+Keys[index];
            var redPresetRow=new WrapPanel();
            foreach(var (fk,st,lbl,tag) in new[]{("red:中距离",3,"中3★","mid3"),("red:泥地",3,"泥3★","dirt3"),("red:英里",3,"英3★","mile3"),("red:长距离",3,"长3★","long3"),("",0,"清零","clear")})
            {
                var pBtn=new Button{Name=$"LocalRoutePreset_{Keys[index]}_{tag}",Content=lbl,FontSize=11,Padding=new Thickness(6,2),Margin=new Thickness(0,2,5,0)};
                pBtn.Click+=(_,_)=>{
                    applying=true;
                    try{if(st>0&&(redFactor.ItemsSource as IEnumerable<Choice>)?.FirstOrDefault(x=>x.Id==fk) is Choice c)redFactor.SelectedItem=c;stars.Value=st;}
                    finally{applying=false;}
                    owner.Changed();
                };
                redPresetRow.Children.Add(pBtn);
            }
            EditorView.Name="PedigreeNodeEditor_"+Keys[index];EditorView.Children.Add(state);editor.Children.Add(kind);editor.Children.Add(search);editor.Children.Add(selection);editor.Children.Add(variant);redGoal.Children.Add(Note("计划培养的红因子；0 星表示未指定，不计入已确认红星"));redGoal.Children.Add(redFactor);redGoal.Children.Add(stars);redGoal.Children.Add(redPresetRow);editor.Children.Add(redGoal);
            ancestorReadOnlyNote.FontSize=11;ancestorReadOnlyNote.IsVisible=false;
            if(index is 1 or 2 or 4 or 5)
            {
                ancestorBudgetBox.Name="LocalRouteAncestorBudgets_"+Keys[index];
                var l4Note=Note("第 4 层曾祖辈红星预算（抬亲代开局适性，如 泥G→泥D / 英B）");l4Note.FontWeight=FontWeight.SemiBold;
                ancestorBudgetBox.Children.Add(l4Note);
                for(var j=0;j<2;j++)ancestorBudgetBox.Children.Add(MakeBudgetRow($"第4层#{j+1}",out l4Factor[j],out l4Stars[j],$"LocalRouteL4_{Keys[index]}_{j}"));
                var l5Stack=new StackPanel{Spacing=4};
                for(var k=0;k<4;k++)l5Stack.Children.Add(MakeBudgetRow($"第5层#{k+1}",out l5Factor[k],out l5Stars[k],$"LocalRouteL5_{Keys[index]}_{k}"));
                l5Expander.Name="LocalRouteL5BudgetExpander_"+Keys[index];
                l5Expander.Content=l5Stack;
                ancestorBudgetBox.Children.Add(l5Expander);
                clearBudgets.Name="LocalRouteClearBudgets_"+Keys[index];
                clearBudgets.Click+=(_,_)=>{applying=true;try{ResetBudgetControls();}finally{applying=false;}owner.Changed();};
                ancestorBudgetBox.Children.Add(clearBudgets);
                editor.Children.Add(ancestorBudgetBox);
            }
            EditorView.Children.Add(editor);EditorView.Children.Add(ancestorReadOnlyNote);EditorView.Children.Add(detail);EditorView.Children.Add(rebreed);EditorView.Children.Add(wins);
            external.Name="LocalRouteExternal_"+Keys[index];EditorView.Children.Add(external);
            external.Click+=(_,_)=>owner.externalPanel.ShowSlot(index,Titles[index]);
            kind.SelectionChanged+=(_,_)=>{if(applying)return;retained=null;RefreshChoices();owner.Changed();};search.TextChanged+=(_,_)=>RefreshChoices();selection.SelectionChanged+=(_,_)=>{if(applying)return;retained=null;RefreshVariants();if(index%3==0&&Kind=="entry"){owner.slots[index+1].ReferenceCharacter=null;owner.slots[index+2].ReferenceCharacter=null;}owner.Changed();};variant.SelectionChanged+=(_,_)=>{if(!applying)owner.Changed();};redFactor.SelectionChanged+=(_,_)=>{if(!applying)owner.Changed();};stars.ValueChanged+=(_,_)=>{if(!applying)owner.Changed();};wins.Click+=async(_,_)=>await owner.EditRaces(index);
            rebreed.Click+=(_,_)=>{var character=CharacterId;Load(new JObject{["kind"]="planned",["character_id"]=character is int id?new JValue(id):JValue.CreateNull(),["variant_id"]=null,["red_target"]=null});owner.Changed();detail.Text="此槽已改为待培养；下面调整的是未来祖辈，原成品记录保持原样。";};RefreshChoices();
        }
        private Control MakeBudgetRow(string lbl,out ComboBox fBox,out ComboBox sBox,string prefix)
        {
            var f=new ComboBox{Name=prefix+"_Factor",HorizontalAlignment=HorizontalAlignment.Stretch};
            var s=new ComboBox{Name=prefix+"_Stars",Width=78,ItemsSource=StarChoices,SelectedIndex=0};
            f.SelectionChanged+=(_,_)=>{
                if(applying)return;
                var hasF=!string.IsNullOrEmpty((f.SelectedItem as Choice)?.Id);
                if(hasF&&s.SelectedIndex==0){applying=true;try{s.SelectedIndex=3;}finally{applying=false;}}
                else if(!hasF&&s.SelectedIndex!=0){applying=true;try{s.SelectedIndex=0;}finally{applying=false;}}
                owner.Changed();
            };
            s.SelectionChanged+=(_,_)=>{if(!applying)owner.Changed();};
            fBox=f;sBox=s;
            var grid=new Grid{ColumnDefinitions=new("56,*,Auto"),ColumnSpacing=6};
            var cap=Note(lbl);cap.VerticalAlignment=VerticalAlignment.Center;cap.FontSize=11;
            grid.Children.Add(cap);Grid.SetColumn(f,1);grid.Children.Add(f);Grid.SetColumn(s,2);grid.Children.Add(s);
            return grid;
        }
        private Choice[] BudgetFactorChoices()=>[new Choice("","未设"),..owner.redFactorChoices];
        private void ResetBudgetControls()
        {
            foreach(var box in l4Factor)if(box!=null)box.SelectedIndex=0;
            foreach(var box in l4Stars)if(box!=null)box.SelectedIndex=0;
            foreach(var box in l5Factor)if(box!=null)box.SelectedIndex=0;
            foreach(var box in l5Stars)if(box!=null)box.SelectedIndex=0;
        }
        private void UpdateAncestorUiVisibility()=>ancestorBudgetBox.IsVisible=(index is 1 or 2 or 4 or 5)&&!locked&&!IsFinished;
        public void Select(bool selected)=>compact.Select(selected);
        public void SetLocked(bool value){var wasLocked=locked;locked=value;if(value&&!wasLocked)ReferenceCharacter=null;editor.IsVisible=external.IsVisible=!value;rebreed.IsVisible=index%3==0&&IsFinished;UpdateAncestorUiVisibility();if(value&&!wasLocked&&ExternalBranch)state.Text="吗哩吗哩祖辈参考";else if(value&&state.Text=="快照资料未齐")state.Text="随成品亲代展开真实快照，核对后显示";RefreshCompact();}
        public void ClearEvidence(){evidence=null;wins.IsEnabled=false;compact.SetAffinityBadge(null);ancestorReadOnlyNote.IsVisible=false;state.Text=ExternalBranch?(locked?"吗哩吗哩祖辈参考":"吗哩吗哩 · 待确认可借"):locked?"随成品亲代展开真实快照，待核对":Kind=="entry"&&owner.ForeignLibrary?"原成品来自另一份库，待重新关联":Kind=="planned"?"待培养（方案）":ReferenceCharacter!=null?"手动参考（未确认持有）":"快照资料未齐 / 账号状态待核对";detail.Text="";RefreshCompact();}
        public void RefreshEntries(){if(Kind=="entry")RefreshChoices();}
        public void RefreshCharacters(){if(Kind=="planned")RefreshChoices();else RefreshRedFactors();RefreshCompact();}
        private void RefreshCompact()
        {
            var character=owner.characters.FirstOrDefault(x=>Number(x["character_id"])==CharacterId);
            var source=ExternalBranch?ExternalIdentity:(selection.SelectedItem as Choice)?.Row;
            var vid=Str(evidence?["variant_id"]);
            if(vid.Length==0)vid=IsFinished||ExternalBranch?Str(source?["variant_id"]):(variant.SelectedItem as Choice)?.Id??"";
            var display=Str(evidence?["display_name"]);
            if(display.Length==0)display=Str(source?["character_name"]??ExternalFact?["character_name"]);
            var clothing=ExternalBranch?Str(source?["variant_name"]):"";
            compact.SetIdentity(character!=null?CharacterLabel(character):display.Length>0?display:
                ExternalBranch?"外部资料待补":ReferenceCharacter is int reference?"未收录角色 "+reference:"点击选择",
                clothing.Length>0?clothing:OutfitName(character,vid),Portrait(character,vid));
            compact.SetState(state.Text??"状态待核对",locked);
            if(Kind=="planned")
            {
                var target=(Read() as JObject)?["red_target"] as JObject;
                compact.SetRed(null,true,planned:target==null?"红因子目标未指定":$"计划 {ShortFactorTag(Str(target["factor_key"]))} {Str(target["stars"])}★");
            }
            else if(ExternalBranch)compact.SetRed(null,true,planned:ExternalRedLabel());
            else if(evidence?["detected_red"] is JObject det)
            {
                var rawLbl=Str(det["label"]);
                var lbl=Str(det["short_label"]).Length>0?Str(det["short_label"]):(rawLbl.Length>0?ShortFactorTag(Str(det["factor_key"]).Length>0?Str(det["factor_key"]):rawLbl):"");
                if((bool?)det["triggers_anti_a"]==true&&lbl.Length>0)lbl="⚠"+lbl;
                compact.SetRed(Number(det["stars"]),(bool?)det["unknown"]!=false,Str(det["conflict"]).Length>0,factorLabel:lbl);
            }
            else
            {
                var red=evidence?["red_summary"] as JObject;
                compact.SetRed(Number(red?["known_stars"]),(bool?)red?["unknown"]!=false,Str(red?["conflict"]).Length>0);
            }
        }
        private string ExternalRedLabel()
        {
            var active=owner.ActiveGoalKeys();
            var totals=(retained?["reference_totals"] as JArray)?.OfType<JObject>().ToArray()??[];
            if(Kind=="external"&&totals.Length>0)
            {
                var hits=(active.Length>0?totals.Where(x=>active.Contains(Str(x["factor_key"]))):totals.Where(x=>Str(x["factor_key"]).StartsWith("red:",StringComparison.Ordinal))).Where(x=>Number(x["family_stars"])!=null).Take(2).ToArray();
                if(hits.Length>0)return string.Join("·",hits.Select(x=>ShortFactorTag(Str(x["factor_key"]))+Number(x["family_stars"])))+"★参考";
            }
            var matching=(ExternalFact?["factors"] as JArray??new()).OfType<JObject>()
                .Where(x=>Str(x["factor_key"]).StartsWith("red:",StringComparison.Ordinal)&&Number(x["stars"])!=null).ToArray();
            if(matching.Length==1)return ShortFactorTag(Str(matching[0]["factor_key"]))+Number(matching[0]["stars"])+"★·参考";
            return "红星资料待补";
        }
        private void RefreshChoices()
        {
            var current=(selection.SelectedItem as Choice)?.Id;var query=(search.Text??"").Trim();var pool=Kind=="entry"?(owner.ForeignLibrary?Array.Empty<JObject>():owner.entries):owner.characters;
            var values=pool.Select(row=>Kind=="entry"?new Choice(Str(row["entry_id"]),string.Join(" · ",new[]{Str(row["character_name"]),Str(row["variant_name"]),Str(row["entry_id"])}.Where(x=>x.Length>0)),row):new Choice(Str(row["character_id"]),CharacterLabel(row),row)).Where(c=>query.Length==0||c.Label.Contains(query,StringComparison.OrdinalIgnoreCase)||(c.Row?["aliases"] as JArray??new()).Any(a=>Str(a).Contains(query,StringComparison.OrdinalIgnoreCase))).ToList();
            var prior=selection.SelectedItem as Choice;if(prior!=null&&values.All(x=>x.Id!=prior.Id)){if(Kind=="entry"&&owner.ForeignLibrary)retained=new JObject{["kind"]="entry",["entry_id"]=prior.Id};else values.Add(prior);}var was=applying;applying=true;try{selection.ItemsSource=values;selection.SelectedItem=values.FirstOrDefault(x=>x.Id==current);selection.IsVisible=search.IsVisible=Kind is "entry" or "planned";variant.IsVisible=redGoal.IsVisible=Kind=="planned";UpdateAncestorUiVisibility();RefreshVariants();RefreshRedFactors();}finally{applying=was;}
        }
        private void RefreshVariants(){var previous=(variant.SelectedItem as Choice)?.Id;var row=(selection.SelectedItem as Choice)?.Row;var values=new[]{new Choice("","衣装未指定")}.Concat((row?["outfits"] as JArray??new()).OfType<JObject>().Select(x=>new Choice(Str(x["variant_id"]),Str(x["name"])+" · "+Str(x["variant_id"]),x))).ToArray();var was=applying;applying=true;try{variant.ItemsSource=values;variant.SelectedItem=values.FirstOrDefault(x=>x.Id==previous)??values[0];}finally{applying=was;}}
        private void RefreshRedFactors()
        {
            var id=(redFactor.SelectedItem as Choice)?.Id??owner.DefaultRedFactorKey();var values=owner.redFactorChoices;redFactor.ItemsSource=values;redFactor.SelectedItem=values.FirstOrDefault(x=>x.Id==id)??values.FirstOrDefault();
            if(index is not (1 or 2 or 4 or 5))return;
            var was=applying;applying=true;
            try
            {
                var bValues=BudgetFactorChoices();
                foreach(var box in l4Factor.Concat(l5Factor))
                {
                    if(box==null)continue;
                    var prev=(box.SelectedItem as Choice)?.Id??"";
                    box.ItemsSource=bValues;
                    box.SelectedItem=bValues.FirstOrDefault(x=>x.Id==prev)??bValues[0];
                }
            }
            finally{applying=was;}
        }
        public JToken? Read(){if(Kind=="external")return retained?.DeepClone();if(Kind=="entry")return selection.SelectedItem is Choice selected?new JObject{["kind"]="entry",["entry_id"]=selected.Id}:retained?.DeepClone();if(Kind=="planned")return new JObject{["kind"]="planned",["character_id"]=int.TryParse((selection.SelectedItem as Choice)?.Id,out var id)?new JValue(id):retained?["character_id"]?.DeepClone()??JValue.CreateNull(),["variant_id"]=StrId(variant),["red_target"]=stars.Value>0?new JObject{["factor_key"]=(redFactor.SelectedItem as Choice)?.Id??owner.DefaultRedFactorKey(),["stars"]=(int)(stars.Value??0)}:JValue.CreateNull()};return null;}
        public void Load(JObject? value){applying=true;try{retained=value==null?null:(JObject)value.DeepClone();kind.SelectedIndex=Str(value?["kind"]) switch{"entry"=>1,"planned"=>2,"external"=>3,_=>0};search.Text="";selection.SelectedItem=null;RefreshChoices();var id=Kind=="entry"?Str(value?["entry_id"]):Str(value?["character_id"]);var values=selection.ItemsSource as IEnumerable<Choice>??[];selection.SelectedItem=values.FirstOrDefault(x=>x.Id==id);RefreshVariants();var vid=Str(value?["variant_id"]);var variants=variant.ItemsSource as IEnumerable<Choice>??[];if(vid.Length>0&&variants.All(x=>x.Id!=vid)){variants=variants.Append(new Choice(vid,"原衣装 #"+vid+"（待核对）")).ToArray();variant.ItemsSource=variants;}variant.SelectedItem=variants.FirstOrDefault(x=>x.Id==vid)??variants.FirstOrDefault();var red=value?["red_target"] as JObject;stars.Value=Number(red?["stars"])??0;var redChoices=redFactor.ItemsSource as IEnumerable<Choice>??[];redFactor.SelectedItem=redChoices.FirstOrDefault(x=>x.Id==Str(red?["factor_key"]))??redChoices.FirstOrDefault();}finally{applying=false;}ClearEvidence();}
        public JObject? ReadAncestorBudget()
        {
            if(index is not (1 or 2 or 4 or 5)||locked||IsFinished)return null;
            var l4Arr=new JArray();var has4=false;
            for(var j=0;j<2;j++)
            {
                var fk=(l4Factor[j]?.SelectedItem as Choice)?.Id??"";
                var st=l4Stars[j]?.SelectedIndex??0;
                if(fk.Length>0&&st>=1&&st<=3){l4Arr.Add(new JObject{["factor_key"]=fk,["stars"]=st});has4=true;}
                else l4Arr.Add(JValue.CreateNull());
            }
            var l5Arr=new JArray();var has5=false;
            for(var k=0;k<4;k++)
            {
                var fk=(l5Factor[k]?.SelectedItem as Choice)?.Id??"";
                var st=l5Stars[k]?.SelectedIndex??0;
                if(fk.Length>0&&st>=1&&st<=3){l5Arr.Add(new JObject{["factor_key"]=fk,["stars"]=st});has5=true;}
                else l5Arr.Add(JValue.CreateNull());
            }
            if(!has4&&!has5)return null;
            var obj=new JObject();if(has4)obj["l4"]=l4Arr;if(has5)obj["l5"]=l5Arr;
            return obj;
        }
        public void LoadBudget(JObject? budget)
        {
            if(index is not (1 or 2 or 4 or 5))return;
            applying=true;
            try
            {
                ResetBudgetControls();
                RefreshRedFactors();
                if(budget?["l4"] is JArray a4)
                {
                    for(var j=0;j<2&&j<a4.Count;j++)
                    {
                        if(a4[j] is not JObject row)continue;
                        var fk=Str(row["factor_key"]);if(fk.StartsWith("pink:",StringComparison.Ordinal))fk="red:"+fk[5..];
                        var st=Number(row["stars"])??0;
                        if(fk.Length>0&&st>=1&&st<=3&&(l4Factor[j].ItemsSource as IEnumerable<Choice>)?.FirstOrDefault(x=>x.Id==fk) is Choice c)
                        {
                            l4Factor[j].SelectedItem=c;l4Stars[j].SelectedIndex=Math.Clamp(st,1,3);
                        }
                    }
                }
                var any5=false;
                if(budget?["l5"] is JArray a5)
                {
                    for(var k=0;k<4&&k<a5.Count;k++)
                    {
                        if(a5[k] is not JObject row)continue;
                        var fk=Str(row["factor_key"]);if(fk.StartsWith("pink:",StringComparison.Ordinal))fk="red:"+fk[5..];
                        var st=Number(row["stars"])??0;
                        if(fk.Length>0&&st>=1&&st<=3&&(l5Factor[k].ItemsSource as IEnumerable<Choice>)?.FirstOrDefault(x=>x.Id==fk) is Choice c)
                        {
                            l5Factor[k].SelectedItem=c;l5Stars[k].SelectedIndex=Math.Clamp(st,1,3);any5=true;
                        }
                    }
                }
                l5Expander.IsExpanded=any5;
            }
            finally{applying=false;}
        }
        private void UpdateAncestorReadOnlyNote(JArray? l4)
        {
            if(index is not (1 or 2 or 4 or 5)||l4==null||Kind!="entry"){ancestorReadOnlyNote.IsVisible=false;return;}
            var parts=new List<string>();
            foreach(var item in l4.OfType<JObject>())
            {
                var cname=Str(item["character_name"]);
                var rf=item["red_factor"] as JObject;
                var rfTxt=rf!=null?$"{Str(rf["label"])}{Number(rf["stars"])??0}★":"红星未记";
                if(cname.Length>0||rf!=null)parts.Add((cname.Length>0?cname:"亲代")+"("+rfTxt+")");
            }
            ancestorReadOnlyNote.IsVisible=parts.Count>0;
            ancestorReadOnlyNote.Text=parts.Count>0?"第4层成品快照："+string.Join(" · ",parts):"";
        }
        public (JArray Layer4,JArray Layer5) BuildClientAncestors(bool parentFinished)
        {
            if(evidence?["layer4_ancestors"] is JArray ev4)
            {
                UpdateAncestorReadOnlyNote(ev4);
                return (ev4,evidence?["layer5_ancestors"] as JArray??new JArray());
            }
            var l4=new JArray();var l5=new JArray();
            var snapParents=Kind=="entry"?(selection.SelectedItem as Choice)?.Row?["parents"] as JArray
                :(ExternalBranch?ExternalEntry?["parents"] as JArray:null);
            for(var j=0;j<2;j++)
            {
                if(snapParents!=null&&j<snapParents.Count&&snapParents[j] is JObject pr)
                {
                    var bestRed=(pr["factors"] as JArray??new()).OfType<JObject>()
                        .Where(x=>(Str(x["kind"]).Length==0||Str(x["kind"])=="red")&&Str(x["factor_key"]).Length>0&&(Number(x["stars"])??0)>0)
                        .OrderByDescending(x=>Number(x["stars"])??0).FirstOrDefault();
                    var rfk=bestRed!=null?Str(bestRed["factor_key"]):"";
                    if(rfk.StartsWith("pink:",StringComparison.Ordinal))rfk="red:"+rfk[5..];
                    var rst=bestRed!=null?(Number(bestRed["stars"])??0):0;
                    var cid=Number(pr["character_id"]);
                    var cname=Str(pr["character_name"]);
                    if(cname.Length==0&&cid!=null&&owner.characters.FirstOrDefault(c=>Number(c["character_id"])==cid) is JObject ch)cname=Str(ch["name"]);
                    l4.Add(new JObject{["slot"]=j,["character_id"]=cid is int idv?new JValue(idv):JValue.CreateNull(),["character_name"]=cname,["red_factor"]=MakeRedBadgeObj(rfk,rst)??(JToken)JValue.CreateNull(),["source"]="library_snapshot"});
                }
                else if(!parentFinished&&!IsFinished&&index is 1 or 2 or 4 or 5)
                {
                    var fk=(l4Factor[j]?.SelectedItem as Choice)?.Id??"";
                    var st=l4Stars[j]?.SelectedIndex??0;
                    l4.Add(new JObject{["slot"]=j,["red_factor"]=MakeRedBadgeObj(fk,st)??(JToken)JValue.CreateNull(),["source"]=fk.Length>0&&st>0?"planned_budget":"empty"});
                }
                else l4.Add(new JObject{["slot"]=j,["red_factor"]=JValue.CreateNull(),["source"]="empty"});
            }
            for(var k=0;k<4;k++)
            {
                if(!parentFinished&&!IsFinished&&index is 1 or 2 or 4 or 5)
                {
                    var fk=(l5Factor[k]?.SelectedItem as Choice)?.Id??"";
                    var st=l5Stars[k]?.SelectedIndex??0;
                    l5.Add(new JObject{["slot"]=k,["red_factor"]=MakeRedBadgeObj(fk,st)??(JToken)JValue.CreateNull(),["source"]=fk.Length>0&&st>0?"planned_budget":"empty"});
                }
                else l5.Add(new JObject{["slot"]=k,["red_factor"]=JValue.CreateNull(),["source"]="empty"});
            }
            UpdateAncestorReadOnlyNote(l4);
            return (l4,l5);
        }
        public void AccumulateDirectRedStars(Dictionary<string,int> conf,Dictionary<string,int> plan)
        {
            if(evidence?["detected_red"] is JObject det&&(bool?)det["unknown"]!=true&&Str(det["factor_key"]) is {Length:>0} dfk&&Number(det["stars"]) is int ds&&ds>0)
            {
                var k=dfk.StartsWith("pink:",StringComparison.Ordinal)?"red:"+dfk[5..]:dfk;
                conf[k]=conf.GetValueOrDefault(k)+ds;
            }
            else if(Kind=="planned"&&stars.Value>0)
            {
                var fk=(redFactor.SelectedItem as Choice)?.Id??"";
                if(fk.Length>0)plan[fk]=plan.GetValueOrDefault(fk)+(int)(stars.Value??0);
            }
            else if(Kind=="entry"&&(selection.SelectedItem as Choice)?.Row?["factors"] is JArray ef)
                AccumulateRedList(ef,conf);
            else if(ExternalBranch&&ExternalFact?["factors"] is JArray xf)
                AccumulateRedList(xf,conf);
        }
        public void ApplyClientVisuals(JArray? clientGrid)
        {
            if(evidence!=null)return;
            compact.SetAptitudeGrid(clientGrid);
            compact.SetAffinityBadge(null);
            compact.SetSubBadge(FormatUpgradeSubBadge(clientGrid));
        }
        public void Evidence(JObject row)
        {
            evidence=(JObject)row.DeepClone();state.Text=Str(row["status_label"]) is string label&&label.Length>0?label:"状态未确认";if(Kind==null&&ReferenceCharacter!=null)state.Text="手动参考（未确认持有）";
            var name=owner.characters.FirstOrDefault(x=>Number(x["character_id"])==Number(row["character_id"]));var race=row["race_summary"] as JObject;
            string redLine;
            if(row["detected_red"] is JObject det)
            {
                var lbl=Str(det["label"]);var dStars=Number(det["stars"]);
                var tag=(bool?)det["triggers_anti_a"]==true?"（⚠ 触发禁升 A 污染！）":
                    (bool?)det["in_avoid_a"]==true?"（命中禁升 A 保护项）":
                    (bool?)det["matches_goal"]==true?"（命中当前目标）":"（非当前目标）";
                redLine=Str(det["conflict"]).Length>0?"实际红因子：记录冲突":
                    (bool?)det["unknown"]==true?(lbl.Length>0?$"实际红因子：{lbl}（星级未知）":"实际红因子：资料未知"):
                    lbl.Length>0?$"实际红因子：{lbl} {dStars}★{tag}":"实际红因子：已确认无红因子";
            }
            else
            {
                var red=row["red_summary"] as JObject;
                redLine="已确认红星："+(Number(red?["known_stars"])?.ToString()??"未知")+((bool?)red?["unknown"]==true?"（仍有未知资料）":"")+(Str(red?["conflict"]).Length>0?"（记录冲突）":"");
            }
            detail.Text=(name==null?"角色资料未齐":CharacterLabel(name))+" · 实际胜场 "+RaceSummaryLabel(race)+"\n"+redLine;
            wins.IsEnabled=!ExternalBranch&&row["record_binding"] is JObject;foreach(var note in (row["notes"] as JArray??new()).Select(Str).Where(s=>s.Length>0))detail.Text+="\n"+note;RefreshCompact();
            compact.SetAptitudeGrid(row["aptitude_grid"] as JArray);
            var ac=row["affinity_contribution"] as JObject;
            var affText=Number(ac?["total"]) is int act?$"+{act}":(Number(ac?["race_bonus"]) is int acrb&&acrb>0?$"+{acrb}胜":null);
            compact.SetAffinityBadge(affText);
            compact.SetSubBadge(FormatUpgradeSubBadge(row["aptitude_grid"] as JArray));
            if(index is 1 or 2 or 4 or 5)
            {
                var l4=row["layer4_ancestors"] as JArray;
                owner.graph?.SetAncestorData(index,l4,row["layer5_ancestors"] as JArray);
                UpdateAncestorReadOnlyNote(l4);
            }
        }
    }

    private void OnExternalPoolChanged()=>autoPairPanel.MarkPoolChanged();
    private Task OnAutoPairPoolMutated()=>externalPanel.RefreshPoolSilent();
    private string DescribeLockedSlots()
    {
        var tags=slots.Where(s=>s.IsLockedCandidate).Select(s=>s.ShortLockLabel()).ToArray();
        return tags.Length==0?"当前无已填槽位":"已锁："+string.Join("、",tags);
    }

    private sealed class AutoPairPanel : Expander
    {
        private readonly Func<JObject,Task<JObject>> request;
        private readonly Func<JObject> context;
        private readonly Action<JObject,JObject> apply;
        private readonly Func<Task> poolMutated;
        private readonly Func<string> lockedSlotSummary;
        private readonly CheckBox includePool=new(){Name="AutoPairIncludeMaliPool",Content="纳入本地吗哩候选池",IsChecked=true,VerticalAlignment=VerticalAlignment.Center};
        private readonly CheckBox respectLocks=new(){Name="AutoPairRespectLocks",Content="锁定当前草稿已填槽位（当前无已填槽位）",IsChecked=true,VerticalAlignment=VerticalAlignment.Center};
        private readonly CheckBox preferWhiteSkills=new(){Name="AutoPairPreferWhiteSkills",Content="祖辈优先白技能（不强求高相性角色）",IsChecked=false,VerticalAlignment=VerticalAlignment.Center};
        private readonly TextBox whiteSkillsInput=new(){Name="AutoPairWhiteSkills",Watermark="祖辈/借马白技能偏好（可选，逗号分隔，如 地固、弯道加速、川崎赛场、逆时针）"};
        private readonly Button runButton=new(){Name="AutoPairRunButton",Content="一键推演三档方案"};
        private readonly Button openMaliTool=new(){Name="AutoPairOpenMaliTool",Content="🌐 内嵌吗哩查借马"};
        private readonly TextBox quickPoolInput=new(){Name="AutoPairQuickPoolInput",Watermark="粘贴吗哩吗哩种马卡片文字或官方搜索结果 JSON，存入后即可参与全局自动配对",AcceptsReturn=true,TextWrapping=TextWrapping.Wrap,MaxLength=262144,MinHeight=60,MaxHeight=150};
        private readonly Button quickSave=new(){Name="AutoPairQuickPoolSave",Content="存入吗哩候选池",FontSize=12};
        private readonly Button quickRefresh=new(){Name="AutoPairQuickPoolRefresh",Content="刷新池数量",FontSize=12};
        private readonly Button quickClear=new(){Name="AutoPairQuickPoolClear",Content="清空候选池",FontSize=12};
        private readonly TextBlock quickPoolStatus=Note("尚未读取本地吗哩候选池（点“刷新池数量”或直接粘贴存入）。");
        private readonly Expander quickPoolExpander;
        private readonly TextBlock status=Note("按上方培育目标、十格适性与 🔒 防污染锁，结合本账号种马库与本地吗哩候选池推演 0 代直出 / 1 代现养 / 2 代精养染色方案。");
        private readonly StackPanel tiersHost=new(){Name="AutoPairTiersHost",Spacing=10};
        private int generation;
        private bool busy;
        private bool poolBusy;
        private bool suppressNextContextClear;
        private bool hadWhiteInput;
        private bool syncingWhiteInput;
        private string? renderedSpecKey;

        public AutoPairPanel(Func<JObject,Task<JObject>> request,Func<JObject> context,Action<JObject,JObject> apply,Func<Task> poolMutated,Func<string> lockedSlotSummary)
        {
            this.request=request;this.context=context;this.apply=apply;this.poolMutated=poolMutated;this.lockedSlotSummary=lockedSlotSummary;
            Name="ParentAutoPairPanel";
            Header="全局自动配对与多代拆解（0代直出 / 1代现养 / 2代精养）";
            IsExpanded=false;

            var topControls=new WrapPanel();
            includePool.Margin=new Thickness(0,0,12,4);
            respectLocks.Margin=new Thickness(0,0,12,4);
            preferWhiteSkills.Margin=new Thickness(0,0,12,4);
            runButton.Margin=new Thickness(0,0,8,4);
            openMaliTool.Margin=new Thickness(0,0,8,4);
            topControls.Children.Add(includePool);
            topControls.Children.Add(respectLocks);
            topControls.Children.Add(preferWhiteSkills);
            topControls.Children.Add(runButton);
            topControls.Children.Add(openMaliTool);

            var quickActions=new WrapPanel();
            foreach(var btn in new[]{quickSave,quickRefresh,quickClear})
            {
                btn.Margin=new Thickness(0,0,8,4);
                quickActions.Children.Add(btn);
            }
            quickPoolExpander=new Expander
            {
                Name="AutoPairQuickPoolExpander",
                Header="本地吗哩候选池快捷录入 / 清理",
                IsExpanded=false,
                Content=new StackPanel{Spacing=6,Children={quickPoolInput,quickActions,quickPoolStatus}},
            };

            Content=new StackPanel
            {
                Spacing=8,
                Children={topControls,whiteSkillsInput,quickPoolExpander,status,tiersHost},
            };

            RefreshLockLabel();
            respectLocks.IsCheckedChanged+=(_,_)=>{RefreshLockLabel();ContextChanged();};
            preferWhiteSkills.IsCheckedChanged+=(_,_)=>ContextChanged();
            whiteSkillsInput.TextChanged+=(_,_)=>
            {
                if(syncingWhiteInput)return;
                var nonEmpty=!string.IsNullOrWhiteSpace(whiteSkillsInput.Text);
                if(nonEmpty&&!hadWhiteInput&&preferWhiteSkills.IsChecked!=true)
                    preferWhiteSkills.IsChecked=true;
                hadWhiteInput=nonEmpty;
                ContextChanged();
            };
            runButton.Click+=async(_,_)=>await RunAutoPair();
            openMaliTool.Click+=(_,_)=>MaliSeedSearchBridge.OpenOrActivate(
                TopLevel.GetTopLevel(this) as Window,
                request,
                ContextWithWhiteTargets,
                ()=>{MarkPoolChanged();_=poolMutated();},
                text=>
                {
                    syncingWhiteInput=true;
                    try
                    {
                        whiteSkillsInput.Text=text;
                        var nonEmpty=!string.IsNullOrWhiteSpace(text);
                        if(nonEmpty&&!hadWhiteInput&&preferWhiteSkills.IsChecked!=true)
                            preferWhiteSkills.IsChecked=true;
                        hadWhiteInput=nonEmpty;
                    }
                    finally{syncingWhiteInput=false;}
                });
            quickSave.Click+=async(_,_)=>await PoolOp("pool_save");
            quickRefresh.Click+=async(_,_)=>await PoolOp("pool_list");
            quickClear.Click+=async(_,_)=>await PoolOp("pool_clear");
        }

        public void RefreshLockLabel()
        {
            var summary=lockedSlotSummary();
            respectLocks.Content=$"锁定当前草稿已填槽位（{summary}）";
        }

        private JObject ContextWithWhiteTargets()
        {
            var snapshot=(JObject)context().DeepClone();
            var terms=MaliSeedSearchBridge.ParseTerms(whiteSkillsInput.Text);
            if(terms.Length>0)
            {
                var rawTargets=snapshot["raw_targets"] as JArray??new JArray();
                var seen=new HashSet<string>(
                    rawTargets.OfType<JObject>().Select(x=>Str(x["name"]).Trim()).Where(x=>x.Length>0),
                    StringComparer.OrdinalIgnoreCase);
                foreach(var term in terms)
                {
                    if(!seen.Add(term))continue;
                    rawTargets.Add(new JObject
                    {
                        ["name"]=term,
                        ["min_stars"]=1,
                        ["priority"]=4,
                    });
                }
                snapshot["raw_targets"]=rawTargets;
            }
            return snapshot;
        }

        private static string SpecKey(JObject snapshot,bool withLocks,bool preferWhite,string whiteText)
        {
            var draft=snapshot["local_route"] as JObject;
            var keyObj=new JObject
            {
                ["target_character_id"]=snapshot["target_character_id"]?.DeepClone()??JValue.CreateNull(),
                ["library_id"]=snapshot["library_id"]?.DeepClone()??JValue.CreateNull(),
                ["account_id"]=snapshot["account_id"]?.DeepClone()??JValue.CreateNull(),
                ["include_unassigned"]=snapshot["include_unassigned"]?.DeepClone()??JValue.CreateNull(),
                ["target_variant_id"]=draft?["target_variant_id"]?.DeepClone()??JValue.CreateNull(),
                ["aptitude_goals"]=draft?["aptitude_goals"]?.DeepClone()??new JArray(),
                ["avoid_a_factors"]=draft?["avoid_a_factors"]?.DeepClone()??new JArray(),
                ["chase_s_factors"]=draft?["chase_s_factors"]?.DeepClone()??new JArray(),
                ["ancestor_red_budgets"]=draft?["ancestor_red_budgets"]?.DeepClone()??new JObject(),
                ["targets"]=snapshot["targets"]?.DeepClone()??new JArray(),
                ["raw_targets"]=snapshot["raw_targets"]?.DeepClone()??new JArray(),
                ["prefer_white_skills"]=preferWhite,
                ["white_skills_text"]=whiteText.Trim(),
            };
            if(withLocks)keyObj["nodes"]=draft?["nodes"]?.DeepClone()??new JArray();
            return keyObj.ToString(Newtonsoft.Json.Formatting.None);
        }

        public void ContextChanged()
        {
            RefreshLockLabel();
            if(suppressNextContextClear)
            {
                suppressNextContextClear=false;
                return;
            }
            if(renderedSpecKey!=null&&renderedSpecKey==SpecKey(ContextWithWhiteTargets(),respectLocks.IsChecked==true,preferWhiteSkills.IsChecked==true,whiteSkillsInput.Text??""))
                return;
            generation++;
            renderedSpecKey=null;
            tiersHost.Children.Clear();
            status.Text="目标、适性、白技能或草稿已更新；点击“一键推演三档方案”重新计算。";
        }

        public void MarkPoolChanged()
        {
            generation++;
            renderedSpecKey=null;
            tiersHost.Children.Clear();
            status.Text="本地吗哩候选池已更新；点击“一键推演三档方案”以纳入最新候选。";
        }

        private JObject PoolCommand(string mode)
        {
            var ctx=context();
            var cmd=new JObject{["action"]="parent_planning_external_compare",["mode"]=mode};
            if(ctx["library_id"] is JToken lib&&lib.Type!=JTokenType.Null)cmd["library_id"]=lib.DeepClone();
            if(ctx["account_id"] is JToken acc&&acc.Type!=JTokenType.Null)cmd["account_id"]=acc.DeepClone();
            return cmd;
        }

        private async Task PoolOp(string mode)
        {
            if(busy||poolBusy)return;
            if(mode=="pool_save"&&string.IsNullOrWhiteSpace(quickPoolInput.Text))
            {
                quickPoolStatus.Text="请先粘贴要存入本地吗哩候选池的卡片文字或 JSON。";
                return;
            }
            poolBusy=true;
            quickSave.IsEnabled=quickRefresh.IsEnabled=quickClear.IsEnabled=false;
            try
            {
                var cmd=PoolCommand(mode);
                if(mode=="pool_save")cmd["candidates"]=quickPoolInput.Text;
                var reply=await request(cmd);
                var count=Number(reply["pool_count"])??0;
                quickPoolExpander.Header=$"本地吗哩候选池快捷录入 / 清理（{count} 匹）";
                quickPoolStatus.Text=mode switch
                {
                    "pool_save"=>$"已存入本地吗哩候选池（新增 {Number(reply["added_count"])??0} 匹，池内共 {count} 匹）。",
                    "pool_clear"=>"已清空本地吗哩候选池。",
                    _=>$"本地吗哩候选池当前共有 {count} 匹候选。"
                };
                if(mode is "pool_save" or "pool_clear")
                {
                    MarkPoolChanged();
                    await poolMutated();
                }
            }
            catch(InvalidOperationException err){quickPoolStatus.Text=err.Message;}
            catch{quickPoolStatus.Text="本地吗哩候选池操作未完成，请稍后重试。";}
            finally
            {
                poolBusy=false;
                quickSave.IsEnabled=quickRefresh.IsEnabled=quickClear.IsEnabled=true;
            }
        }

        private async Task RunAutoPair()
        {
            if(busy||poolBusy)return;
            var snapshot=ContextWithWhiteTargets();
            if(snapshot["target_character_id"]?.Type!=JTokenType.Integer)
            {
                status.Text="请先在上方选择育成目标马娘，再进行一键三档推演。";
                return;
            }
            var version=generation;
            var withLocks=respectLocks.IsChecked==true;
            var preferWhite=preferWhiteSkills.IsChecked==true;
            var whiteText=whiteSkillsInput.Text??"";
            var command=(JObject)snapshot.DeepClone();
            command["action"]="parent_planning_route_evaluate";
            command["mode"]="auto_pair";
            command["include_mali_pool"]=includePool.IsChecked==true;
            command["respect_locked_slots"]=withLocks;
            command["prefer_white_skills"]=preferWhite;
            command["limit"]=3;

            busy=true;
            runButton.IsEnabled=false;
            status.Text=preferWhite
                ?"正在全局检索自有库与吗哩候选池（祖辈优先白技能模式），并拆解三档方案…"
                :"正在全局检索自有库与吗哩候选池，并拆解三档方案…";
            tiersHost.Children.Clear();
            try
            {
                var reply=await request(command);
                if(version!=generation||!JToken.DeepEquals(snapshot,ContextWithWhiteTargets()))return;
                renderedSpecKey=SpecKey(snapshot,withLocks,preferWhite,whiteText);
                RenderResult(reply,snapshot,version,withLocks,preferWhite,whiteText);
            }
            catch(InvalidOperationException err){if(version==generation)status.Text=err.Message;}
            catch{if(version==generation)status.Text="自动配对推演未完成，请检查目标与网络/本地资料后重试。";}
            finally{busy=false;runButton.IsEnabled=true;}
        }

        private void RenderResult(JObject reply,JObject snapshot,int version,bool withLocks,bool preferWhite,string whiteText)
        {
            var inv=reply["inventory_summary"] as JObject??new();
            var owned=Number(inv["owned_count"])??0;
            var borrowed=Number(inv["borrowed_count"])??0;
            var refCount=Number(inv["reference_count"])??0;
            var poolCount=Number(inv["mali_pool_count"])??0;
            quickPoolExpander.Header=$"本地吗哩候选池快捷录入 / 清理（{poolCount} 匹）";
            var refPart=refCount>0?$" · 参考 {refCount} 匹":"";
            var whiteModeTag=(bool?)inv["prefer_white_skills"]==true?" · 🎯祖辈优先白技能":"";
            status.Text=$"已按当前目标完成全局推演（自有 {owned} 匹 · 可借 {borrowed} 匹{refPart} · 本地吗哩池 {poolCount} 匹{whiteModeTag}）。";

            tiersHost.Children.Clear();
            foreach(var key in new[]{"tier0_direct","tier1_one_gen","tier2_two_gen"})
            {
                if(reply[key] is not JObject tierObj)continue;
                tiersHost.Children.Add(BuildTierSection(tierObj,snapshot,version,withLocks,preferWhite,whiteText));
            }
        }

        private Control BuildTierSection(JObject tierObj,JObject snapshot,int version,bool withLocks,bool preferWhite,string whiteText)
        {
            var title=Str(tierObj["title"]);
            var note=Str(tierObj["note"]);
            var rows=(tierObj["suggestions"] as JArray??new()).OfType<JObject>().ToArray();
            var content=new StackPanel{Spacing=8};
            if(note.Length>0)content.Children.Add(Note(note));
            if(rows.Length==0)
            {
                content.Children.Add(Note("当前条件下暂无符合要求的候选方案。"));
            }
            else
            {
                foreach(var row in rows)
                    content.Children.Add(BuildSuggestionCard(row,snapshot,version,withLocks,preferWhite,whiteText));
            }
            return new Expander
            {
                Header=$"{title}（{rows.Length} 个推荐）",
                IsExpanded=rows.Length>0,
                Content=content,
            };
        }

        private Control BuildSuggestionCard(JObject row,JObject snapshot,int version,bool withLocks,bool preferWhite,string whiteText)
        {
            var route=row["local_route"] as JObject;
            var localReview=row["local_review"] as JObject;
            var condReview=row["conditional_review"] as JObject;
            var body=new StackPanel{Spacing=4};

            var head=new TextBlock
            {
                Text=$"[{Str(row["tier_label"])}] {Str(row["label"])}",
                FontWeight=FontWeight.SemiBold,
                TextWrapping=TextWrapping.Wrap,
            };
            body.Children.Add(head);

            var summary=Str(row["summary"]);
            if(summary.Length>0)body.Children.Add(Note(summary));

            var apt=row["aptitude_summary"] as JObject??new();
            var knownStars=Number(apt["known_goal_stars"])??0;
            var plannedStars=Number(apt["planned_red_stars"])??0;
            var reqStars=Number(apt["required_goal_stars"])??0;
            var remGap=Number(apt["remaining_gap"])??0;
            var hasS=(bool?)apt["has_s_target"]==true;
            var sStars=Number(apt["s_goal_stars"])??0;
            var aptBadge=hasS
                ?((bool?)apt["satisfied_now"]==true
                    ?$"开局 A 达标（{knownStars}/{reqStars}★ · S 目标红星 {sStars}★）"
                    :(bool?)apt["satisfied_with_plan"]==true
                        ?$"S 冲刺规划（开局 {knownStars}+{plannedStars}/{reqStars}★ · S 目标红星 {sStars}★）"
                        :$"仍缺 {remGap}★（开局 {knownStars}+{plannedStars}/{reqStars}★ · S 红星 {sStars}★）")
                :((bool?)apt["satisfied_now"]==true
                    ?$"适性已满（已确认 {knownStars}/{reqStars}★）"
                    :(bool?)apt["satisfied_with_plan"]==true
                        ?$"规划达标（现有 {knownStars}★ + 计划 {plannedStars}★ / 目标 {reqStars}★）"
                        :$"仍缺 {remGap}★（现有 {knownStars}★ + 计划 {plannedStars}★ / 目标 {reqStars}★）");

            var anti=row["anti_a_summary"] as JObject??new();
            var violated=(anti["violated_labels"] as JArray??new()).Select(Str).Where(x=>x.Length>0).ToArray();
            var antiBadge=(bool?)anti["polluted"]==true
                ?"⚠ 污染：" + (violated.Length>0?string.Join("、",violated):Str(anti["status"]))
                :Str(anti["status"])=="inactive"?"未设防污染锁":"🔒 防污染安全";

            var comp=row["compatibility"] as JObject??new();
            var compTotal=Number(comp["total"]);
            var compSub=Number(comp["known_subtotal"]);
            var compGrade=Str(comp["grade"]);
            var raceAff=comp["race_affinity"] as JObject;
            var raceBonus=Number(raceAff?["bonus_points"])??0;
            string compBadge;
            if(raceAff!=null&&raceBonus>0)
            {
                var totalWithBonus=Number(raceAff["total_with_race_bonus"])??((compTotal??compSub??0)+raceBonus);
                var gradeSym=GradeSymbol(Str(raceAff["grade_with_race_bonus"]) is {Length:>0} gb?gb:compGrade);
                var basePart=compTotal is int ct?ct.ToString():$"已知{compSub??0}";
                compBadge=$"相性 {totalWithBonus} {gradeSym}（基础{basePart}+G1胜鞍{raceBonus}）";
            }
            else
            {
                compBadge=compTotal is int ct
                    ?$"基础相性 {ct}"+(compGrade.Length>0?$"（{compGrade}）":"")
                    :$"已知相性 {compSub??0}";
            }

            var badges=new List<string>{aptBadge,antiBadge,compBadge};
            var fac=row["factor_summary"] as JObject;
            if(fac!=null&&(Number(fac["total_targets"])??0)>0)
            {
                badges.Add($"目标因子 {Number(fac["satisfied_count"])??0}/{Number(fac["total_targets"])??0} 达标 · 确认 {Number(fac["confirmed_stars"])??0}★");
            }
            body.Children.Add(Note(string.Join(" · ",badges)));

            var hitWhite=(fac?["checks"] as JArray??new()).OfType<JObject>()
                .Where(c=>(bool?)c["satisfied"]==true||(Number(c["known_stars"])??0)>0)
                .Select(c=>$"{Str(c["name"]).Replace("原文：","")}{(Number(c["known_stars"]) is int ks&&ks>0?ks+"★":"")}")
                .Where(s=>s.Length>0)
                .ToArray();
            if(hitWhite.Length>0)
                body.Children.Add(Note("🎯 命中目标白技能："+string.Join(" · ",hitWhite)));

            foreach(var step in (row["breeding_steps"] as JArray??new()).Select(Str).Where(x=>x.Length>0))
                body.Children.Add(Note("• "+step));

            var issues=(condReview?["selection_issues"] as JArray??new()).Select(Str).Where(x=>x.Length>0).Distinct().ToArray();
            foreach(var issue in issues)
                body.Children.Add(Note("⚠ "+issue));

            var actionRow=new WrapPanel{Margin=new Thickness(0,4,0,0)};
            var applyBtn=new Button
            {
                Content="应用此方案到六槽草稿",
                IsEnabled=route!=null&&(localReview??condReview)!=null,
                Margin=new Thickness(0,0,6,2),
            };
            applyBtn.Click+=(_,_)=>
            {
                if(version!=generation||SpecKey(snapshot,withLocks,preferWhite,whiteText)!=SpecKey(ContextWithWhiteTargets(),withLocks,preferWhiteSkills.IsChecked==true,whiteSkillsInput.Text??""))
                {
                    status.Text="草稿、目标、白技能或候选池已变化，请重新点击“一键推演三档方案”。";
                    return;
                }
                if(route==null||(localReview??condReview) is not JObject review)return;
                suppressNextContextClear=true;
                apply((JObject)route.DeepClone(),(JObject)review.DeepClone());
                suppressNextContextClear=false;
                if(withLocks)renderedSpecKey=SpecKey(ContextWithWhiteTargets(),true,preferWhiteSkills.IsChecked==true,whiteSkillsInput.Text??"");
                status.Text=$"已将「{Str(row["label"])}」填入上方六槽草稿；也可继续从下方切换其他推荐。";
            };
            actionRow.Children.Add(applyBtn);
            foreach(var cTask in (condReview?["tasks"] as JArray??new()).OfType<JObject>())
            {
                if(cTask["recommended_schedule"] is not JObject cSched||cSched["pacer_preset"] is not JObject cPreset)continue;
                var cRawNode=Str(cTask["node"]).Length>0?Str(cTask["node"]):Str(cTask["node_key"]);
                var cSlotIdx=Number(cTask["slot_index"])??Array.IndexOf(Keys,cRawNode);
                var slotName=SlotShortName(cSlotIdx>=0?cSlotIdx:0);
                var cCount=Number(cSched["race_count"])??0;
                var copySchedBtn=new Button
                {
                    Content=$"📋 复制{slotName} Pacer赛程({cCount}场G1)",
                    FontSize=12,
                    Margin=new Thickness(0,0,6,2),
                };
                copySchedBtn.Click+=async(_,_)=>await CopySchedulePresetAsync(this,status,slotName,cPreset,cCount);
                actionRow.Children.Add(copySchedBtn);
            }
            body.Children.Add(actionRow);

            return new Border
            {
                Padding=new Thickness(9),
                BorderThickness=new Thickness(1),
                CornerRadius=new CornerRadius(8),
                BorderBrush=(bool?)anti["polluted"]==true||issues.Length>0
                    ?new SolidColorBrush(Color.Parse("#D97706"))
                    :Brushes.Gray,
                Child=body,
            };
        }
    }
}
