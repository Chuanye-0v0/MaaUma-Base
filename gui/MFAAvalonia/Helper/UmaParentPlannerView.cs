using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Controls.Documents;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

/// <summary>Native local-material pane beside author websites. No web-to-native API bridge.</summary>
public sealed class UmaParentPlannerView : UserControl
{
    private static readonly (string Key,string Label,string Origin,string Url)[] Sites = [
        ("umadna","umDNA 主页","https://umadna.com","https://umadna.com/"),
        ("umadna","umDNA 技能页","https://umadna.com","https://umadna.com/skill_preview.html"),
        ("614","614 技能筛选","https://614tools.top","https://614tools.top/skillselect/")];
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<string,Control>? fakeBrowserFactory;
    private readonly Func<Task<JArray>>? fakeReadTargets;
    private readonly Dictionary<string,Control> browsers = new(StringComparer.Ordinal);
    private readonly Dictionary<string,string> requestedUrls = new(StringComparer.Ordinal);
    private readonly Grid webStage = new();
    private readonly Grid webPane = new() { RowDefinitions=new("Auto,*"),Margin=new Thickness(0,10,0,0) };
    private readonly Grid localPane = new() { RowDefinitions=new("Auto,*,Auto"),Margin=new Thickness(0,10,0,0) };
    private readonly TabControl plannerTabs = new() { Name="ParentPlannerSections",SelectedIndex=0,
        HorizontalContentAlignment=HorizontalAlignment.Stretch,VerticalContentAlignment=VerticalAlignment.Stretch,
        ItemsPanel=new FuncTemplate<Panel?>(()=>new WrapPanel()) };
    private readonly TextBlock webStatus = Label("选择作者原站后联网打开；本地种马库不会自动传给网页。",12);
    private readonly TextBlock status = Label("读取本地目标目录…",12);
    private readonly TextBox input = new() { Name="ParentPlannerInput",Watermark="技能或因子名，支持逗号分隔；从候选确认" };
    private readonly ListBox suggestions = new() { Name="ParentPlannerSuggestions",MaxHeight=150,IsVisible=false };
    private readonly StackPanel selectedList = new() { Name="ParentPlannerTargets",Spacing=4 };
    private readonly List<JObject> targets = new();
    private readonly List<JObject> webSelections = new();
    private JObject[] catalog = [];
    private readonly ComboBox scope = new() { Name="ParentPlannerScope",ItemsSource=new[]{"本体＋两亲代","仅本体"},SelectedIndex=0,Width=150 };
    private readonly Button evaluate = new() { Name="ParentPlannerEvaluate",Content="查找本地素材" };
    private readonly Button readTargets = new() { Name="ParentPlannerReadWebTargets",Content="读取网页目标",IsEnabled=false };
    private readonly StackPanel results = new() { Name="ParentPlannerResults",Spacing=8 };
    private string selectedSite="",selectedUrl="";
    private bool busy,loaded;

    public UmaParentPlannerView(Func<JObject,Task<JObject>> request,Func<string,Control>? browserFactory=null,Func<Task<JArray>>? readWebTargets=null)
    {
        this.request=request;fakeBrowserFactory=browserFactory;fakeReadTargets=readWebTargets;
        this.Bind(TextElement.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        var root=new Grid { RowDefinitions=new("Auto,*"),Margin=new Thickness(16),RowSpacing=7 };
        var heading=new StackPanel { Spacing=3 };heading.Children.Add(Label("种马规划",22));
        heading.Children.Add(Label("本地查看种马素材、基本相性与技能来源；组合参考仅比较库内成品，不代表继承概率。",12));root.Children.Add(heading);
        var toolbar=new StackPanel { Spacing=5 };var siteButtons=new WrapPanel { Orientation=Orientation.Horizontal };
        foreach(var site in Sites) { var button=new Button { Content=site.Label,Margin=new Thickness(0,0,6,3) };button.Click+=(_,_)=>OpenSite(site.Key,site.Url);siteButtons.Children.Add(button); }
        var external=new Button { Content="作者原站 ↗",Margin=new Thickness(0,0,6,3) };external.Click+=(_,_)=> { if(selectedUrl.Length>0)Process.Start(new ProcessStartInfo(selectedUrl){UseShellExecute=true}); };siteButtons.Children.Add(external);
        ToolTip.SetTip(readTargets,"尝试读取umDNA主页已选技能；网页改版后未匹配的目标，可直接在本地规划搜索添加。攻略页和614可复制名称添加。");readTargets.Click+=async(_,_)=>await ReadWebTargets();siteButtons.Children.Add(readTargets);toolbar.Children.Add(siteButtons);toolbar.Children.Add(webStatus);webPane.Children.Add(toolbar);
        webStage.Children.Add(Label("请选择 umDNA 或 614 作者原站。网页只按你的操作联网；你可复制目标名称到“本地规划”添加。",14));Grid.SetRow(webStage,1);webPane.Children.Add(webStage);
        var editor=new StackPanel { Spacing=5 };editor.Children.Add(Label("本地目标清单",18));editor.Children.Add(input);
        var add=new Button { Content="确认名称",Name="ParentPlannerConfirm" };add.Click+=(_,_)=>ConfirmNames();
        var goalActions=new WrapPanel { Orientation=Orientation.Horizontal };goalActions.Children.Add(add);scope.Margin=new Thickness(6,0,0,0);goalActions.Children.Add(scope);editor.Children.Add(goalActions);
        suggestions.ItemTemplate=new FuncDataTemplate<JObject>((row,_)=>Label((bool?)row?["raw"]==true?(Text(row,"web_id").Length>0?"未映射网页ID "+Text(row,"web_id")+" · ":"")+"按原文匹配："+Text(row,"name"):Display(row)+" · "+(Text(row,"kind")=="skill"?"技能":"因子"),13));
        suggestions.SelectionChanged+=(_,_)=> { if(suggestions.SelectedItem is not JObject row)return;AddTarget(row);var pending=Tokens(input.Text);input.Text=string.Join("，",pending.Skip(1));UpdateSuggestions(); };
        editor.Children.Add(suggestions);editor.Children.Add(Label("每项依次为最低星数、优先级（1低～5高）。",11));editor.Children.Add(new ScrollViewer { Content=selectedList,MaxHeight=220,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled });
        editor.Children.Add(evaluate);editor.Children.Add(status);localPane.Children.Add(editor);
        var resultScroll=new ScrollViewer { Name="ParentPlannerResultScroll",Content=results,Margin=new Thickness(0,8,0,0),HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };Grid.SetRow(resultScroll,1);localPane.Children.Add(resultScroll);
        var footer=Label("素材信息只在本机读取。星数未知会单独标记，不算作没有。",11);footer.Margin=new Thickness(0,5,0,0);Grid.SetRow(footer,2);localPane.Children.Add(footer);
        plannerTabs.ItemsSource=new[] {
            new TabItem {Header="种马素材",Content=localPane},
            new TabItem {Header="配种与相性",Content=new UmaParentCompatibilityView(request,CurrentPlanningGoals,RestorePlanningGoals)},
            new TabItem {Header="技能查询",Content=new UmaSkillReferenceView(request,AddMappedSkillTarget)},
            new TabItem {Header="作者工具",Content=webPane} };
        Grid.SetRow(plannerTabs,1);root.Children.Add(plannerTabs);Content=root;
        input.TextChanged+=(_,_)=>UpdateSuggestions();input.KeyDown+=(_,e)=> { if(e.Key==Avalonia.Input.Key.Enter){e.Handled=true;ConfirmNames();} };
        evaluate.Click+=async(_,_)=>await Evaluate();Loaded+=async(_,_)=>{if(!loaded)await LoadCatalog();};
    }
    private JObject CurrentPlanningGoals()=>new() {
        ["targets"]=new JArray(targets.Where(t=>Text(t,"target_id").Length>0).Select(t=>new JObject {
            ["target_id"]=t["target_id"]?.DeepClone(),["min_stars"]=t["min_stars"]?.DeepClone(),["priority"]=t["priority"]?.DeepClone()})),
        ["raw_targets"]=new JArray(targets.Where(t=>Text(t,"target_id").Length==0).Select(t=>new JObject {
            ["name"]=t["name"]?.DeepClone(),["min_stars"]=t["min_stars"]?.DeepClone(),["priority"]=t["priority"]?.DeepClone()})) };
    private void RestorePlanningGoals(JObject saved)
    {
        targets.Clear();input.Text="";results.Children.Clear();
        foreach(var row in (saved["targets"] as JArray??new()).OfType<JObject>()) {
            var id=Text(row,"target_id");if(id.Length==0)continue;
            var source=catalog.FirstOrDefault(item=>Text(item,"target_id")==id);
            var restored=source==null?new JObject{["target_id"]=id,["name"]="未收录目标 · "+id}:(JObject)source.DeepClone();
            restored["min_stars"]=row["min_stars"]?.DeepClone()??1;restored["priority"]=row["priority"]?.DeepClone()??3;targets.Add(restored);
        }
        foreach(var row in (saved["raw_targets"] as JArray??new()).OfType<JObject>()) {
            var restored=(JObject)row.DeepClone();restored["raw"]=true;restored["min_stars"]??=1;restored["priority"]??=3;targets.Add(restored);
        }
        RenderTargets();UpdateSuggestions();status.Text=$"已恢复方案的 {targets.Count} 个目标；素材结果需要重新计算。";
    }
    private bool AddMappedSkillTarget(string targetId)
    {
        var match=catalog.SingleOrDefault(row=>Text(row,"target_id")==targetId);
        plannerTabs.SelectedIndex=0;
        if(match==null){status.Text="该技能暂未对应本地规划目录，请先核对目录或手动选择目标。";return false;}
        AddTarget(match);status.Text="已加入目标清单；可继续设置最低星数和优先级。";return true;
    }
    private static string Text(JObject? row,string key)=>row?[key]?.Type==JTokenType.String?(string?)row[key]??"":"";
    private static string Display(JObject? row)=>Text(row,"display_name").Length>0?Text(row,"display_name"):Text(row,"name");
    private static TextBlock Label(string text,int size=13)=>new(){Text=text,FontSize=size,TextWrapping=TextWrapping.Wrap};
    private static string[] Tokens(string? text)=>(text??"").Split([',','，','\n','\r'],StringSplitOptions.RemoveEmptyEntries).Select(s=>s.Trim()).Where(s=>s.Length>0).ToArray();
    private static string Normal(string text)=>new(text.Normalize(NormalizationForm.FormKC).Where(c=>!char.IsWhiteSpace(c)&&!char.IsPunctuation(c)).Select(char.ToLowerInvariant).ToArray());
    private static IEnumerable<string> Names(JObject row)=>new[]{Text(row,"name")}.Concat((row["aliases"] as JArray??new()).Values<string>().OfType<string>());
    private void Lock(bool value){busy=value;input.IsEnabled=scope.IsEnabled=evaluate.IsEnabled=selectedList.IsEnabled=suggestions.IsEnabled=!value;}
    public async Task LoadCatalog()
    {
        Lock(true);try { var response=await request(new JObject { ["action"]="parent_planner_catalog" });catalog=(response["targets"] as JArray??new()).OfType<JObject>().Where(x=>Text(x,"target_id").Length>0).ToArray();loaded=true;status.Text="填写目标后确认候选，再查找素材。";UpdateSuggestions(); }
        catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="目标目录暂未读取成功，请稍后重试。";}finally{Lock(false);}
    }
    private void UpdateSuggestions()
    {
        suggestions.SelectedItem=null;var token=Tokens(input.Text).FirstOrDefault();
        if(token==null){suggestions.ItemsSource=Array.Empty<JObject>();suggestions.IsVisible=false;return;}
        var imported=webSelections.Where(x=>Text(x,"name")==token).ToArray();
        var ids=imported.Select(x=>"skill:"+Text(x,"id")).ToHashSet(StringComparer.Ordinal);
        var normal=Normal(token);var choices=catalog.Where(x=>ids.Contains(Text(x,"target_id"))).Concat(catalog.Where(x=>Names(x).Any(name=>Normal(name).Contains(normal,StringComparison.Ordinal))))
            .GroupBy(x=>Text(x,"target_id")).Select(x=>x.First()).Take(15).ToList();
        var unrecognized=imported.FirstOrDefault(x=>!catalog.Any(c=>Text(c,"target_id")=="skill:"+Text(x,"id")));
        choices.Add(new JObject { ["name"]=token,["raw"]=true,["web_id"]=unrecognized?["id"] });suggestions.ItemsSource=choices;suggestions.IsVisible=true;
    }
    public void ConfirmNames()
    {
        var pending=new List<string>();foreach(var token in Tokens(input.Text)) {
            var imported=webSelections.Where(x=>Text(x,"name")==token).ToArray();
            var matches=imported.Length>0?catalog.Where(x=>imported.Any(w=>Text(x,"target_id")=="skill:"+Text(w,"id"))).ToArray():catalog.Where(x=>Names(x).Any(name=>Normal(name)==Normal(token))).ToArray();
            if(matches.Length==1)AddTarget(matches[0]);else pending.Add(token);
        }
        input.Text=string.Join("，",pending);UpdateSuggestions();if(pending.Count>0)status.Text="还有名称需要选择目录候选，或明确按原文匹配。";
    }
    private void AddTarget(JObject value)
    {
        var id=Text(value,"target_id");var name=Text(value,"name");if(targets.Any(x=>id.Length>0?Text(x,"target_id")==id:Text(x,"target_id").Length==0&&Text(x,"name")==name))return;
        var target=(JObject)value.DeepClone();target["min_stars"]=1;target["priority"]=3;targets.Add(target);RenderTargets();
    }
    private void RenderTargets()
    {
        selectedList.Children.Clear();foreach(var target in targets.ToArray()) {
            var row=new Grid { ColumnDefinitions=new("*,68,68,Auto"),ColumnSpacing=4 };
            var name=Label((bool?)target["raw"]==true?"原文 · "+Text(target,"name"):Display(target),12);name.VerticalAlignment=VerticalAlignment.Center;row.Children.Add(name);
            var stars=new NumericUpDown { Value=(int)target["min_stars"]!,Minimum=1,Maximum=9,Increment=1,FormatString="0",Name="ParentPlannerStars" };ToolTip.SetTip(stars,"目标最低星数");stars.ValueChanged+=(_,_)=>target["min_stars"]=(int)(stars.Value??1);Grid.SetColumn(stars,1);row.Children.Add(stars);
            var priority=new NumericUpDown { Value=(int)target["priority"]!,Minimum=1,Maximum=5,Increment=1,FormatString="0",Name="ParentPlannerPriority" };ToolTip.SetTip(priority,"目标优先级：1低，5高");priority.ValueChanged+=(_,_)=>target["priority"]=(int)(priority.Value??3);Grid.SetColumn(priority,2);row.Children.Add(priority);
            var remove=new Button { Content="×",Padding=new Thickness(6,2) };remove.Click+=(_,_)=>{targets.Remove(target);RenderTargets();};Grid.SetColumn(remove,3);row.Children.Add(remove);selectedList.Children.Add(row);
        }
    }
    public async Task Evaluate()
    {
        if(busy)return;if(targets.Count==0){status.Text="请先确认至少一个目标。";return;}if(Tokens(input.Text).Length>0){status.Text="还有名称未确认，请选择候选或清空输入。";return;}
        var command=new JObject { ["action"]="parent_planner_evaluate",["scope"]=scope.SelectedIndex==1?"self":"pedigree",
            ["targets"]=new JArray(targets.Where(t=>Text(t,"target_id").Length>0).Select(t=>new JObject { ["target_id"]=t["target_id"],["min_stars"]=t["min_stars"],["priority"]=t["priority"] })),
            ["raw_targets"]=new JArray(targets.Where(t=>Text(t,"target_id").Length==0).Select(t=>new JObject { ["name"]=t["name"],["min_stars"]=t["min_stars"],["priority"]=t["priority"] })) };
        Lock(true);try { RenderResults(await request(command));status.Text="已按当前目标读取本地素材。"; }
        catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="本地素材暂未读取成功，请重试。";}finally{Lock(false);}
    }
    private void RenderResults(JObject response)
    {
        results.Children.Clear();results.Children.Add(Label(Text(response,"message"),12));
        var missing=(response["missing_targets"] as JArray??new()).OfType<JObject>().ToArray();
        if(missing.Length>0){var lines=new StackPanel{Spacing=5};lines.Children.Add(Label("待补目标",16));foreach(var row in missing){lines.Children.Add(Label(Text(row,"name")+" · "+Text(row,"message"),12));if(row["sources"] is JObject sources)lines.Children.Add(SourceReferences(sources));}results.Children.Add(Card(lines));}
        var materials=(response["materials"] as JArray??new()).OfType<JObject>().ToArray();results.Children.Add(Label($"本地素材 · {materials.Length} 匹",16));
        foreach(var row in materials){var lines=new StackPanel{Spacing=4};lines.Children.Add(Label(Text(row,"title"),15));
            void List(string key,string label){var values=(row[key] as JArray??new()).Select(x=>x is JObject item?Text(item,"name"):x.ToString()).Where(x=>x.Length>0).ToArray();if(values.Length>0)lines.Children.Add(Label(label+string.Join("、",values),12));}
            List("matched_targets","可用：");
            var unknownNames=(row["uncertain_targets"] as JArray??new()).Select(x=>x.ToString()).ToHashSet(StringComparer.Ordinal);
            var confirmedMissing=(row["missing_targets"] as JArray??new()).Select(x=>x.ToString()).Where(x=>!unknownNames.Contains(x)).ToArray();
            if(confirmedMissing.Length>0)lines.Children.Add(Label("待补："+string.Join("、",confirmedMissing),12));List("uncertain_targets","未确认：");
            var completeness=Text(row,"completeness");if(completeness.Length>0)lines.Children.Add(Label(completeness switch{"complete"=>"已读全","partial"=>"因子未读全","insufficient_data"=>"资料不足",_=>completeness},11));
            var explanation=row["rank_explanation"];if(explanation!=null)ToolTip.SetTip(lines,explanation.Type==JTokenType.Array?string.Join("\n",explanation.Select(x=>x.ToString())):explanation.ToString());results.Children.Add(Card(lines));}
        if(materials.Length==0)results.Children.Add(Label("当前库里没有可展示的素材；可以先确认种马库中的记录。",12));
    }
    private static Control SourceReferences(JObject sources)
    {
        var cards=(sources["support_cards"] as JArray??new()).OfType<JObject>().ToArray();
        var trainees=(sources["trainees"] as JArray??new()).OfType<JObject>().ToArray();
        var expander=new Expander{Header=$"补种来源参考 · 马娘 {trainees.Length} / 支援卡 {cards.Length}",IsExpanded=false};
        expander.PropertyChanged+=(_,e)=>{
            if(e.Property!=Expander.IsExpandedProperty||!expander.IsExpanded||expander.Content!=null)return;
            var body=new StackPanel{Spacing=5};body.Children.Add(Label("静态来源，持卡与事件条件请核对。",12));
            var skills=(sources["skills"] as JArray??new()).OfType<JObject>().Select(x=>Text(x,"name")).Where(x=>x.Length>0).ToArray();
            if(skills.Length>0)body.Children.Add(Label("对应技能："+string.Join("、",skills),12));
            body.Children.Add(Label("补种马娘",14));
            if(trainees.Length==0)body.Children.Add(Label("暂未整理对应马娘来源。",12));
            foreach(var row in trainees){var rank=(int?)row["awakening_rank"];body.Children.Add(Label(Text(row,"name")+(Text(row,"outfit").Length>0?" · "+Text(row,"outfit"):"")+(rank>0?$" · 觉醒 {rank}":""),12));}
            body.Children.Add(Label("支援卡",14));
            if(cards.Length==0)body.Children.Add(Label("暂未整理对应支援卡来源。",12));
            foreach(var row in cards){var title=string.Join(" · ",new[]{Text(row,"rarity"),Text(row,"name"),Text(row,"type")}.Where(x=>x.Length>0));body.Children.Add(Label(title,12));
                foreach(var key in new[]{"methods","event_notes"}){var notes=(row[key] as JArray??new()).Select(x=>x.ToString()).Where(x=>x.Length>0).ToArray();if(notes.Length>0)body.Children.Add(Label(string.Join("；",notes),11));}}
            foreach(var note in (sources["source_notes"] as JArray??new()).Select(x=>x.ToString()))body.Children.Add(Label(note,11));
            expander.Content=new ScrollViewer{Content=body,MaxHeight=270,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled};
        };
        return expander;
    }
    private static GlassCard Card(Control content)=>new(){Content=content,Padding=new Thickness(10),CornerRadius=new CornerRadius(9),IsAnimated=false,IsOpaque=true};
    public void OpenSite(string key,string url)
    {
        var site=Sites.FirstOrDefault(x=>x.Key==key&&x.Url==url);if(site.Key==null)throw new InvalidOperationException("不支持这个规划器地址。");
        if(!browsers.TryGetValue(key,out var control)) { if(browsers.Count==0)webStage.Children.Clear();
            if(fakeBrowserFactory!=null)control=fakeBrowserFactory(key);else { var host=new UmaParentPlannerBrowser(site.Origin,Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"MaaUma","web-tools","parent-planner-"+key));host.StatusChanged+=text=>{if(selectedSite==key){webStatus.Text=text+(!CanReadTargets(host.CurrentUrl)?" · 可复制目标名称后手动添加":"");readTargets.IsEnabled=CanReadTargets(host.CurrentUrl);}};control=host; }
            browsers[key]=control;webStage.Children.Add(control);
        }
        foreach(var pair in browsers)pair.Value.IsVisible=pair.Key==key;selectedSite=key;selectedUrl=url;readTargets.IsEnabled=key=="umadna"&&CanReadTargets(url);
        webStatus.Text=key=="umadna"?"umDNA 作者原站 · umadna.com · 仅网页联网，不传本地库":"614 作者原站 · 614tools.top · 仅网页联网，不传本地库";
        if(!requestedUrls.TryGetValue(key,out var requested)||requested!=url){if(control is UmaParentPlannerBrowser browser)browser.Navigate(url);requestedUrls[key]=url;}plannerTabs.SelectedIndex=3;
    }
    public async Task ReadWebTargets()
    {
        if(selectedSite!="umadna")return;
        try { JArray rows;if(fakeReadTargets!=null)rows=await fakeReadTargets();else if(browsers.TryGetValue("umadna",out var control)&&control is UmaParentPlannerBrowser browser){
                if(!CanReadTargets(browser.CurrentUrl))throw new InvalidOperationException("读取目标目前支持umDNA主页；攻略页和614可复制名称后手动添加。");
                rows=await browser.EvaluateReadOnlyTargetsAsync(UmadnaReadTargetsScript);
            }else return;
            webSelections.Clear();webSelections.AddRange(rows.OfType<JObject>().Where(x=>Text(x,"id").Length>0&&Text(x,"id").All(char.IsDigit)&&Text(x,"name").Length>0).Select(x=>(JObject)x.DeepClone()));
            var names=webSelections.Select(x=>Text(x,"name")).Distinct().ToArray();if(names.Length==0){status.Text="未读到可导入的目标。可以直接在本地规划搜索技能或因子，也可复制网页名称添加。";plannerTabs.SelectedIndex=0;return;}
            input.Text=string.Join("，",names);UpdateSuggestions();var unmapped=webSelections.Count(x=>!catalog.Any(c=>Text(c,"target_id")=="skill:"+Text(x,"id")));status.Text=$"读取 {names.Length} 个网页目标"+(unmapped>0?$"，{unmapped} 个ID未映射":"")+"；请确认候选后查找本地素材。";plannerTabs.SelectedIndex=0;
        }catch(InvalidOperationException e){webStatus.Text=status.Text=e.Message;plannerTabs.SelectedIndex=0;}catch{webStatus.Text=status.Text="网页目标暂未读取成功，可手动复制名称到本地目标清单。";plannerTabs.SelectedIndex=0;}
    }
    internal static bool CanReadTargets(string? url)=>Uri.TryCreate(url,UriKind.Absolute,out var uri)
        &&UmaParentPlannerBrowser.IsAllowedUrl(new Uri("https://umadna.com/"),url)&&uri.AbsolutePath=="/";
    // Verified homepage selected-target container; read only, no storage/API or DOM writes.
    internal const string UmadnaReadTargetsScript="(() => { const root = document.querySelector('#ppTargets'); if (!root) return {unsupported: 'target_selector_missing'}; return Array.from(root.querySelectorAll(':scope > .pp-chip[data-t]')).map(el => ({id: el.getAttribute('data-t') || '', name: (el.querySelector('span')?.textContent || '').trim()})).filter(x => /^[0-9]+$/.test(x.id) && x.name.length > 0); })()";
}
