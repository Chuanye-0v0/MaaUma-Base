using System;
using System.Linq;
using System.Collections.Generic;
using System.Threading.Tasks;
using Avalonia.Controls;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaParentCompatibilityView
{
    private ParentLocalRouteView? localRouteEditor;
    private bool syncingLocalRoute;
    private readonly Dictionary<int,JObject> familyRouteEvidence=new();
    private string familyEvidenceContext="";
    private int familyEvidenceRevision=-1;
    private bool FamilyEvidenceCurrent=>familyEvidenceRevision==revision&&familyEvidenceContext==FamilyEvidenceContext;
    private string FamilyEvidenceContext=>string.Join("|",AccountId??"",libraryId??"",target.SelectedId?.ToString()??"",(parentA.SelectedItem as EntryChoice)?.Id??"",(parentB.SelectedItem as EntryChoice)?.Id??"",manual.ToString(),(includeUnassigned.IsChecked==true).ToString());
    private Control BuildLocalRouteSection()
    {
        localRouteEditor=new ParentLocalRouteView(request,()=>new JObject{
            ["target_character_id"]=target.SelectedId is int id?new JValue(id):JValue.CreateNull(),
            ["account_id"]=AccountId==null?JValue.CreateNull():new JValue(AccountId),
            ["account_label"]=(account.SelectedItem as AccountChoice)?.Label??"资料参考",
            ["library_id"]=planLibraryId==null?(libraryId==null?JValue.CreateNull():new JValue(libraryId)):new JValue(planLibraryId),
            ["current_library_id"]=libraryId==null?JValue.CreateNull():new JValue(libraryId),
            ["library_revision"]=libraryRevision is int libraryVersion?new JValue(libraryVersion):JValue.CreateNull(),
            ["include_unassigned"]=includeUnassigned.IsChecked==true},
            ()=>new string?[]{(parentA.SelectedItem as EntryChoice)?.Id,(parentB.SelectedItem as EntryChoice)?.Id},
            async()=>await LoadCatalog());
        localRouteEditor.SetTargetEditor(target);localRouteEditor.DraftChanged+=()=>{if(!applying&&!syncingLocalRoute)SyncLegacyFromRoute();};
        return localRouteEditor;
    }
    private void SyncLegacyFromRoute(bool preserveLibrary=false)
    {
        if(localRouteEditor==null||syncingLocalRoute)return;var was=applying;syncingLocalRoute=applying=true;
        try{var route=localRouteEditor.ReadDraft();var specs=route["nodes"] as JArray??new();var a=specs.ElementAtOrDefault(0) as JObject;var b=specs.ElementAtOrDefault(3) as JObject;var aid=Text(a?["kind"])=="entry"?Text(a?["entry_id"]):null;var bid=Text(b?["kind"])=="entry"?Text(b?["entry_id"]):null;
            if(!preserveLibrary&&planLibraryId==null)planLibraryId=libraryId;var sameLibrary=planLibraryId==null||planLibraryId==libraryId;
            parentA.SelectedItem=sameLibrary?entries.FirstOrDefault(e=>e.Id==aid):null;parentB.SelectedItem=sameLibrary?entries.FirstOrDefault(e=>e.Id==bid):null;
            manual=localRouteEditor.HasManualReferences||specs.OfType<JObject>().Any(s=>Text(s["kind"])=="planned");unresolvedParentIds=!manual&&((aid!=null&&parentA.SelectedItem==null)||(bid!=null&&parentB.SelectedItem==null))?new[]{aid,bid}:[];
            var lineage=localRouteEditor.LegacyLineage();for(var i=0;i<6;i++)nodes[i].SetId(lineage[i]??(preserveLibrary?nodes[i].SelectedId:null));
        }finally{applying=was;syncingLocalRoute=false;}
        if(!applying)Invalidate();
    }
    private void SyncLocalRouteContext()=>localRouteEditor?.SetEntries(allEntries.Select(x=>x.Row).ToArray());
    private string FamilyAvailability(int index,EntryChoice? entry,bool isManual)
    {
        if(isManual)return "手动参考（未确认持有）";
        if(entry==null)return "快照资料未齐";
        if(FamilyEvidenceCurrent&&familyRouteEvidence.TryGetValue(index,out var evidence)){var label=Text(evidence["status_label"]);if(label.Length>0)return label;}
        if(index%3==0)return "账号状态待核对";
        var snapshot=(entry.Row["parents"] as JArray)?.ElementAtOrDefault(index%3-1) as JObject;
        return snapshot==null||Id(snapshot["character_id"])==null?"快照资料未齐":"随亲代继承 · 真实快照（未确认单独持有）";
    }
    private Control FamilyRecordActions(int index,Control card)
    {
        if(!FamilyEvidenceCurrent||!familyRouteEvidence.TryGetValue(index,out var row)||row["record_binding"] is not JObject binding)return card;
        var panel=new Avalonia.Controls.StackPanel{Spacing=4};panel.Children.Add(card);var edit=new Button{Name="CompatActualWins_"+NodeKeys[index],Content="编辑实际胜场"};edit.Click+=async(_,_)=>{if(localRouteEditor!=null)await localRouteEditor.EditRecordBinding(binding,NodeLabels[index],TopLevel.GetTopLevel(edit) as Window);};panel.Children.Add(edit);return panel;
    }
    private async Task RefreshFamilyAvailability()
    {
        if(!UsesEntries||target.SelectedId is not int selectedTarget)return;var key=FamilyEvidenceContext;var version=revision;
        try{var response=await request(new JObject{["action"]="parent_planning_route_evaluate",["target_character_id"]=selectedTarget,["library_id"]=libraryId==null?JValue.CreateNull():new JValue(libraryId),["account_id"]=AccountId==null?JValue.CreateNull():new JValue(AccountId),["include_unassigned"]=includeUnassigned.IsChecked==true,["local_route"]=new JObject{["aptitude_goal"]=new JObject{["factor_key"]="red:泥地",["baseline_rank"]=null,["target_rank"]="A",["target_variant_id"]=null},["nodes"]=new JArray(new JObject{["kind"]="entry",["entry_id"]=((EntryChoice)parentA.SelectedItem!).Id},null,null,new JObject{["kind"]="entry",["entry_id"]=((EntryChoice)parentB.SelectedItem!).Id},null,null)}});
            if(version!=revision||key!=FamilyEvidenceContext)return;familyRouteEvidence.Clear();foreach(var row in (response["route_nodes"] as JArray??new()).OfType<JObject>())if(Id(row["index"]) is int index&&index>=0&&index<6)familyRouteEvidence[index]=(JObject)row.DeepClone();familyEvidenceContext=key;familyEvidenceRevision=version;RefreshFamily();
        }catch{familyRouteEvidence.Clear();familyEvidenceContext="";RefreshFamily();}
    }
}
