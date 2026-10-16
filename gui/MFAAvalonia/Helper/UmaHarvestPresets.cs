using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Data;
using Avalonia.Data.Converters;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Extensions.MaaFW;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>One named-list editor; selection belongs to Harvest's secondary menu.</summary>
public static class UmaHarvestPresets
{
    private sealed record Choice(string Id,string Name) { public override string ToString()=>Name; }
    private static string StorePath => Path.Combine(UmaPublicRuntime.BackendRoot,"runs","harvest-presets.json");

    public static void Apply(List<MaaProcessor.NodeAndParam> nodes)
    {
        if(!File.Exists(StorePath))return;
        var store=JObject.Parse(File.ReadAllText(StorePath));var selected=store["selection"]!;
        var skill=((JArray)store["skills"]!).FirstOrDefault(r=>(string?)r["id"]==(string?)selected["skill_list_id"]);
        var factor=((JArray)store["factors"]!).FirstOrDefault(r=>(string?)r["id"]==(string?)selected["factor_profile_id"]);
        if(skill==null && factor==null && (int?)selected["rerolls"]==0)return;
        var snapshot=new JObject { ["version"]=1,["skill_list"]=skill?.DeepClone(),["factor_profile"]=factor?.DeepClone(),
            ["rerolls"]=selected["rerolls"]!.DeepClone(),["tp_budget"]=selected["tp_budget"]!.DeepClone(),
            ["jewel_budget"]=selected["jewel_budget"]!.DeepClone() };
        foreach(var node in nodes.Where(n=>n.Entry=="UmaHarvest")) {
            var value=JToken.Parse(node.Param ?? "[]");IEnumerable<JToken> entries=value is JArray a ? a.Children() : new[]{value};
            JObject? target=null;
            foreach(var entry in entries)if(entry["UmaHarvest"] is JObject harvest) {
                target=harvest;
                // A preset is one atomic snapshot. Injecting it into every
                // override makes Newtonsoft's merge concatenate its arrays.
                if(harvest["custom_action_param"] is JObject oldOptions)oldOptions.Remove("preset");
            }
            if(target!=null) {
                if(target["custom_action_param"] is not JObject)target["custom_action_param"]=new JObject();
                ((JObject)target["custom_action_param"]!)["preset"]=snapshot.DeepClone();
            } else {
                var extra=new JObject { ["UmaHarvest"]=new JObject { ["custom_action_param"]=new JObject { ["preset"]=snapshot.DeepClone() } } };
                if(value is JArray array)array.Add(extra);else value=new JArray(value,extra);
            }
            node.Param=value.ToString(Formatting.None);
        }
    }

    private static TextBlock Text(string value,int size=13)=>new() { Text=value,FontSize=size,TextWrapping=TextWrapping.Wrap };
    private static NumericUpDown Number(decimal value,decimal max)=>new() { Minimum=0,Maximum=max,Value=value,Width=100 };
    private static Choice? Selected(ComboBox box)=>box.SelectedItem as Choice;

    public static Control CreateControls(Func<bool> running,bool continuous=false)
    {
        var root=new StackPanel { Spacing=10,Margin=new Thickness(0,8,0,20) };
        root.Bind(Control.IsEnabledProperty,new Binding("IsRunning") {
            Converter=new FuncValueConverter<bool,bool>(value=>!value) });
        var title=Text("收菜技能清单与因子方案",16);title.FontWeight=FontWeight.Bold;root.Children.Add(title);
        var skill=new ComboBox { HorizontalAlignment=HorizontalAlignment.Stretch };
        var factor=new ComboBox { HorizontalAlignment=HorizontalAlignment.Stretch };
        var status=Text("读取清单…");var manage=new Button { Content="管理技能清单 / 因子方案…",HorizontalAlignment=HorizontalAlignment.Stretch };
        JObject? store=null;bool applying=false;
        void Render(JObject data) {
            applying=true;store=data;
            skill.ItemsSource=new[]{new Choice("","通用策略（不使用清单）")}.Concat(((JArray)data["skills"]!).Select(r=>new Choice((string)r["id"]!,(string)r["name"]!))).ToArray();
            factor.ItemsSource=new[]{new Choice("","保留初次因子（不使用方案）")}.Concat(((JArray)data["factors"]!).Select(r=>new Choice((string)r["id"]!,(string)r["name"]!))).ToArray();
            skill.SelectedItem=((IEnumerable<Choice>)skill.ItemsSource!).First(c=>c.Id==(string?)data["selection"]!["skill_list_id"]);
            factor.SelectedItem=((IEnumerable<Choice>)factor.ItemsSource!).First(c=>c.Id==(string?)data["selection"]!["factor_profile_id"]);
            applying=false;status.Text=continuous ? "清单优先，剩余PT通用补学；每轮学完后结算因子。这里的重抽TP和萝卜上限为每轮上限，连续任务另有本次总上限。" : "清单优先，剩余PT通用补学。请将下面的技能学习设为“学习技能后结束”。设置在启动时固定。";
        }
        async Task Refresh() {
            try { var data=await UmaPublicRuntime.Request(new JObject { ["action"]="load" },"desktop.umaa_public_presets");Render((JObject)data[0]!["store"]!); }
            catch(Exception error) { status.Text="清单读取失败："+error.Message; }
        }
        async Task Select() {
            if(applying || store==null)return;
            if(running()) {status.Text="任务正在运行，清单切换将在停止后操作。";return;}
            var draft=(JObject)store.DeepClone();draft["selection"]!["skill_list_id"]=Selected(skill)?.Id ?? "";
            draft["selection"]!["factor_profile_id"]=Selected(factor)?.Id ?? "";
            skill.IsEnabled=factor.IsEnabled=manage.IsEnabled=false;
            try { var data=await UmaPublicRuntime.Request(new JObject { ["action"]="save",["store"]=draft },"desktop.umaa_public_presets");Render((JObject)data[0]!["store"]!); }
            catch(Exception error) {Render(store);status.Text="清单未保存："+error.Message;}
            finally {skill.IsEnabled=factor.IsEnabled=manage.IsEnabled=true;}
        }
        root.Children.Add(Text("优先学习清单"));root.Children.Add(skill);root.Children.Add(Text("因子权重方案"));root.Children.Add(factor);
        root.Children.Add(manage);root.Children.Add(UmaSeedCycle.CreateGoalControls(running));root.Children.Add(status);
        skill.SelectionChanged+=async(_,_)=>await Select();factor.SelectionChanged+=async(_,_)=>await Select();
        manage.Click+=async(_,_)=> {
            if(running()){status.Text="请等本轮任务停止后再修改收菜清单。";return;}
            if(TopLevel.GetTopLevel(root) is Window owner) {
                try {await ShowEditor(owner);await Refresh();}
                catch(Exception error) {status.Text="编辑器未打开："+error.Message;}
            }
        };
        root.AttachedToVisualTree+=async(_,_)=>await Refresh();
        return root;
    }

    public static async Task ShowEditor(Window owner)
    {
        var data=await UmaPublicRuntime.Request(new JObject { ["action"]="load" },"desktop.umaa_public_presets");
        var store=(JObject)data[0]!["store"]!.DeepClone();var catalog=(JObject)data[0]!["catalog"]!;
        var skills=((JArray)catalog["skills"]!).Cast<JObject>().ToArray();var factors=((JArray)catalog["factors"]!).Cast<JObject>().ToArray();
        var bySkill=skills.ToDictionary(r=>(string)r["id"]!);var byFactor=factors.ToDictionary(r=>(string)r["key"]!);
        var window=new Window { Title="收菜清单管理 · 技能与因子",Width=1100,Height=800,MinWidth=850,MinHeight=620,
            WindowStartupLocation=WindowStartupLocation.CenterOwner };
        var layout=new DockPanel { Margin=new Thickness(20) };var footer=new StackPanel { Orientation=Orientation.Horizontal,Spacing=12,Margin=new Thickness(0,14,0,0) };
        var save=new Button { Content="保存清单与设置" };var close=new Button { Content="取消 / 关闭" };var hint=Text("清单只表达偏好；实际购买仍由当前页面、PT和现有执行器决定。");
        footer.Children.Add(save);footer.Children.Add(close);footer.Children.Add(hint);DockPanel.SetDock(footer,Dock.Bottom);layout.Children.Add(footer);
        var tabs=new TabControl();layout.Children.Add(tabs);window.Content=layout;
        var skillPanel=new DockPanel();var factorPanel=new DockPanel();
        tabs.ItemsSource=new[]{new TabItem { Header="技能清单",Content=skillPanel },new TabItem { Header="因子权重 / 重抽",Content=factorPanel }};
        JObject? currentSkill=null;JObject? currentFactor=null;bool choosing=false;

        StackPanel Header(string kind,out ComboBox presets,out TextBox name,out Button add,out Button duplicate) {
            var bar=new StackPanel { Spacing=8,Margin=new Thickness(0,8,0,12) };
            presets=new ComboBox { HorizontalAlignment=HorizontalAlignment.Stretch };name=new TextBox { Watermark=kind=="skills" ? "例如 英里、中距离、技能清单1" : "例如 英里种马、耐力三星" };
            add=new Button { Content="新建" };duplicate=new Button { Content="复制当前清单" };
            var actions=new StackPanel { Orientation=Orientation.Horizontal,Spacing=8 };actions.Children.Add(add);actions.Children.Add(duplicate);
            bar.Children.Add(presets);bar.Children.Add(name);bar.Children.Add(actions);return bar;
        }
        var skillHeader=Header("skills",out var skillPresets,out var skillName,out var newSkill,out var copySkill);
        DockPanel.SetDock(skillHeader,Dock.Top);skillPanel.Children.Add(skillHeader);
        var factorHeader=Header("factors",out var factorPresets,out var factorName,out var newFactor,out var copyFactor);
        DockPanel.SetDock(factorHeader,Dock.Top);factorPanel.Children.Add(factorHeader);
        var skillBody=new Grid { ColumnDefinitions=new("*,*,Auto"),ColumnSpacing=14 };
        var availablePanel=new DockPanel();var selectedPanel=new DockPanel();
        var search=new TextBox { Watermark="搜索技能名称、说明、国服/日服ID" };DockPanel.SetDock(search,Dock.Top);availablePanel.Children.Add(search);
        var filter=new ComboBox { ItemsSource=new[]{"全部","英里","中距离","短距离","长距离","通用","金技能","仅已接入购买器"},SelectedIndex=0,Margin=new Thickness(0,8,0,8) };
        DockPanel.SetDock(filter,Dock.Top);availablePanel.Children.Add(filter);
        var available=new ListBox { SelectionMode=SelectionMode.Multiple };availablePanel.Children.Add(available);
        available.ItemTemplate=new FuncDataTemplate<JObject>((row,_)=> {
            var panel=new StackPanel();panel.Children.Add(Text((string?)row?["name"] ?? "",14));
            var rarity=(string?)row?["rarity"] switch { "gold"=>"金技能", "unique"=>"继承固有", "normal"=>"普通技能", _=>"技能" };
            var detail=Text(rarity+" · "+(string?)row?["description"]+((bool?)row?["supported"]==true ? "" : " · 当前购买器未覆盖"),11);
            detail.MaxHeight=44;panel.Children.Add(detail);return panel;
        });
        var selected=new ListBox();DockPanel.SetDock(Text("优先顺序：上方先学，剩余点数通用补学。"),Dock.Top);
        var selectedLabel=Text("优先顺序：上方先学，剩余点数通用补学。");DockPanel.SetDock(selectedLabel,Dock.Top);selectedPanel.Children.Add(selectedLabel);selectedPanel.Children.Add(selected);
        var skillActions=new StackPanel { Spacing=10,VerticalAlignment=VerticalAlignment.Center };
        var addSkills=new Button { Content="添加 →" };var removeSkill=new Button { Content="移出清单" };var up=new Button { Content="上移" };var down=new Button { Content="下移" };
        foreach(var b in new[]{addSkills,removeSkill,up,down})skillActions.Children.Add(b);
        skillBody.Children.Add(availablePanel);Grid.SetColumn(selectedPanel,1);skillBody.Children.Add(selectedPanel);Grid.SetColumn(skillActions,2);skillBody.Children.Add(skillActions);skillPanel.Children.Add(skillBody);
        void SkillRows() {
            selected.ItemsSource=(currentSkill?["skill_ids"] as JArray ?? new JArray()).Values<string>().Select(id=>new Choice(id!, (string?)bySkill.GetValueOrDefault(id!)?["name"] ?? id!)).ToArray();
        }
        void FilterSkills() {
            var text=(search.Text ?? "").Trim();var key=filter.SelectedIndex switch {1=>"mile",2=>"middle",3=>"short",4=>"long",5=>"any",_=>""};
            available.ItemsSource=skills.Where(r=> ((string)r["name"]!+" "+(string?)r["description"]+" "+(string?)r["db_id"]).Contains(text,StringComparison.OrdinalIgnoreCase)
                && (key=="" || ((JArray)r["distances"]!).Values<string>().Contains(key))
                && (filter.SelectedIndex!=6 || (string?)r["rarity"]=="gold") && (filter.SelectedIndex!=7 || (bool?)r["supported"]==true)).ToArray();
        }
        search.TextChanged+=(_,_)=>FilterSkills();filter.SelectionChanged+=(_,_)=>FilterSkills();
        addSkills.Click+=(_,_)=> {
            if(currentSkill==null){hint.Text="请先新建一个技能清单。";return;}
            var ids=(JArray)currentSkill["skill_ids"]!;
            foreach(var row in available.SelectedItems?.Cast<JObject>() ?? Array.Empty<JObject>())if(!ids.Values<string>().Contains((string)row["id"]!))ids.Add((string)row["id"]!);
            SkillRows();
        };
        removeSkill.Click+=(_,_)=> {if(selected.SelectedItem is Choice choice && currentSkill!=null){var ids=(JArray)currentSkill["skill_ids"]!;ids.Remove(ids.First(t=>(string)t==choice.Id));SkillRows();}};
        void Move(int delta) {if(currentSkill==null)return;var index=selected.SelectedIndex;var ids=(JArray)currentSkill["skill_ids"]!;
            if(index<0 || index+delta<0 || index+delta>=ids.Count)return;var row=ids[index];ids.RemoveAt(index);ids.Insert(index+delta,row);SkillRows();selected.SelectedIndex=index+delta;}
        up.Click+=(_,_)=>Move(-1);down.Click+=(_,_)=>Move(1);

        var factorBody=new Grid { ColumnDefinitions=new("*,*"),ColumnSpacing=14 };var left=new DockPanel();var right=new DockPanel();
        var factorSearch=new TextBox { Watermark="搜索蓝、红、白、绿因子" };DockPanel.SetDock(factorSearch,Dock.Top);left.Children.Add(factorSearch);
        var factorList=new ListBox { SelectionMode=SelectionMode.Multiple };left.Children.Add(factorList);
        factorList.ItemTemplate=new FuncDataTemplate<JObject>((row,_)=>Text((string?)row?["name"]+" · "+(string?)row?["kind"]));
        var factorToolbar=new StackPanel { Spacing=6,Margin=new Thickness(0,0,0,10) };
        var addFactors=new Button { Content="添加选中因子 →" };var import=new Button { Content="从技能清单导入白因子" };var importSkill=new ComboBox { HorizontalAlignment=HorizontalAlignment.Stretch };
        var custom=new TextBox { Watermark="库里没有？输入游戏里的完整因子名" };var addCustom=new Button { Content="添加自定义因子" };
        foreach(var control in new Control[]{addFactors,importSkill,import,custom,addCustom})factorToolbar.Children.Add(control);
        factorToolbar.Children.Add(Text("评分 = 各因子权重 × 星级系数；低于门槛不计分。"));
        var starPanel=new StackPanel { Orientation=Orientation.Horizontal,Spacing=8 };var starFields=new[]{Number(1,1000000),Number(2,1000000),Number(3,1000000)};
        for(var i=0;i<3;i++){starPanel.Children.Add(Text((i+1)+"星"));starPanel.Children.Add(starFields[i]);}
        factorToolbar.Children.Add(starPanel);DockPanel.SetDock(factorToolbar,Dock.Top);right.Children.Add(factorToolbar);
        var weighted=new StackPanel { Spacing=7 };right.Children.Add(new ScrollViewer { Content=weighted });
        factorBody.Children.Add(left);Grid.SetColumn(right,1);factorBody.Children.Add(right);factorPanel.Children.Add(factorBody);
        var budget=new StackPanel { Spacing=6,Margin=new Thickness(0,10,0,0) };var budgetLine=new StackPanel { Orientation=Orientation.Horizontal,Spacing=10 };
        var rerolls=Number((decimal)store["selection"]!["rerolls"]!,3);var tp=Number((decimal)store["selection"]!["tp_budget"]!,9999);var jewels=Number((decimal)store["selection"]!["jewel_budget"]!,9999);
        foreach(var c in new Control[]{Text("额外重抽次数"),rerolls,Text("TP总上限"),tp,Text("萝卜总上限"),jewels})budgetLine.Children.Add(c);
        budget.Children.Add(budgetLine);budget.Children.Add(Text("0表示不额外消耗。月卡的免费机会也计入重抽次数；费用以实际弹窗为准。不自动补TP，不买资源。未识别的重抽/比较页面不会盲点。"));
        DockPanel.SetDock(budget,Dock.Bottom);factorPanel.Children.Insert(0,budget);
        void WeightRows() {
            weighted.Children.Clear();if(currentFactor==null)return;
            foreach(var rule in ((JArray)currentFactor["weights"]!).Cast<JObject>().ToArray()) {
                var key=(string)rule["key"]!;var line=new StackPanel { Orientation=Orientation.Horizontal,Spacing=8 };
                var label=Text((string?)byFactor.GetValueOrDefault(key)?["name"] ?? key.Replace("custom:",""));label.Width=130;line.Children.Add(label);
                var weight=Number((decimal)rule["weight"]!,1000000);weight.Width=90;
                var minimum=Number((decimal)rule["min_stars"]!,3);minimum.Minimum=1;minimum.Width=60;
                var remove=new Button { Content="移除" };line.Children.Add(weight);line.Children.Add(Text("至少"));line.Children.Add(minimum);line.Children.Add(Text("星"));line.Children.Add(remove);
                weight.ValueChanged+=(_,_)=>rule["weight"]=weight.Value ?? 0;minimum.ValueChanged+=(_,_)=>rule["min_stars"]=(int)(minimum.Value ?? 1);
                remove.Click+=(_,_)=> {((JArray)currentFactor["weights"]!).Remove(rule);WeightRows();};weighted.Children.Add(line);
            }
        }
        void AddFactor(string key) {if(currentFactor==null){hint.Text="请先新建因子方案。";return;}var weights=(JArray)currentFactor["weights"]!;
            if(!weights.Any(r=>(string?)r["key"]==key))weights.Add(new JObject { ["key"]=key,["weight"]=10,["min_stars"]=1 });}
        addFactors.Click+=(_,_)=> {foreach(var row in factorList.SelectedItems?.Cast<JObject>() ?? Array.Empty<JObject>())AddFactor((string)row["key"]!);WeightRows();};
        addCustom.Click+=(_,_)=> {if(!string.IsNullOrWhiteSpace(custom.Text)){AddFactor("custom:"+custom.Text.Trim());custom.Text="";WeightRows();}};
        import.Click+=(_,_)=> {
            var source=((JArray)store["skills"]!).FirstOrDefault(r=>(string?)r["id"]==Selected(importSkill)?.Id);
            if(source!=null)foreach(var id in ((JArray)source["skill_ids"]!).Values<string>())if((string?)bySkill.GetValueOrDefault(id!)?["factor_key"] is string key)AddFactor(key);
            WeightRows();
        };
        factorSearch.TextChanged+=(_,_)=>factorList.ItemsSource=factors.Where(r=>((string)r["name"]!+" "+(string)r["kind"]!).Contains(factorSearch.Text ?? "",StringComparison.OrdinalIgnoreCase)).ToArray();
        void PresetChoices(ComboBox box,string kind,JObject? selectedRow) {
            box.ItemsSource=((JArray)store[kind]!).Select(r=>new Choice((string)r["id"]!,(string)r["name"]!)).ToArray();
            box.SelectedItem=((IEnumerable<Choice>)box.ItemsSource!).FirstOrDefault(r=>r.Id==(string?)selectedRow?["id"]);
            importSkill.ItemsSource=((JArray)store["skills"]!).Select(r=>new Choice((string)r["id"]!,(string)r["name"]!)).ToArray();
        }
        void ChooseSkill() {currentSkill=((JArray)store["skills"]!).Cast<JObject>().FirstOrDefault(r=>(string?)r["id"]==Selected(skillPresets)?.Id);choosing=true;skillName.Text=(string?)currentSkill?["name"];choosing=false;SkillRows();}
        void ChooseFactor() {currentFactor=((JArray)store["factors"]!).Cast<JObject>().FirstOrDefault(r=>(string?)r["id"]==Selected(factorPresets)?.Id);choosing=true;factorName.Text=(string?)currentFactor?["name"];
            for(var i=0;i<3;i++)starFields[i].Value=(decimal?)currentFactor?["star_weights"]?[i] ?? i+1;choosing=false;WeightRows();}
        skillPresets.SelectionChanged+=(_,_)=>ChooseSkill();factorPresets.SelectionChanged+=(_,_)=>ChooseFactor();
        skillName.TextChanged+=(_,_)=>{if(!choosing && currentSkill!=null)currentSkill["name"]=skillName.Text ?? "";};
        factorName.TextChanged+=(_,_)=>{if(!choosing && currentFactor!=null)currentFactor["name"]=factorName.Text ?? "";};
        for(var i=0;i<3;i++){var index=i;starFields[i].ValueChanged+=(_,_)=>{if(!choosing && currentFactor!=null)currentFactor["star_weights"]![index]=starFields[index].Value ?? 0;};}
        void New(string kind,bool copy) {
            var previous=kind=="skills" ? currentSkill : currentFactor;var rows=(JArray)store[kind]!;
            var row=copy && previous!=null ? (JObject)previous.DeepClone() : kind=="skills" ?
                new JObject { ["skill_ids"]=new JArray() } : new JObject { ["weights"]=new JArray(),["star_weights"]=new JArray(1,2,3) };
            row["id"]=Guid.NewGuid().ToString("N");row["name"]=copy && previous!=null ? (string?)previous["name"]+" 副本" : (kind=="skills" ? "技能清单" : "因子方案")+(rows.Count+1);rows.Add(row);
            if(kind=="skills"){currentSkill=row;PresetChoices(skillPresets,kind,row);}else{currentFactor=row;PresetChoices(factorPresets,kind,row);}
        }
        newSkill.Click+=(_,_)=>New("skills",false);copySkill.Click+=(_,_)=>New("skills",true);newFactor.Click+=(_,_)=>New("factors",false);copyFactor.Click+=(_,_)=>New("factors",true);
        save.Click+=async(_,_)=> {
            store["selection"]!["rerolls"]=(int)(rerolls.Value ?? 0);store["selection"]!["tp_budget"]=(int)(tp.Value ?? 0);store["selection"]!["jewel_budget"]=(int)(jewels.Value ?? 0);
            save.IsEnabled=false;
            try {await UmaPublicRuntime.Request(new JObject { ["action"]="save",["store"]=store },"desktop.umaa_public_presets");hint.Text="已保存；回到收菜二级菜单选择要使用的清单。";}
            catch(Exception error) {hint.Text="未保存："+error.Message;}
            finally {save.IsEnabled=true;}
        };
        close.Click+=(_,_)=>window.Close();
        PresetChoices(skillPresets,"skills",((JArray)store["skills"]!).FirstOrDefault() as JObject);
        PresetChoices(factorPresets,"factors",((JArray)store["factors"]!).FirstOrDefault() as JObject);
        FilterSkills();factorList.ItemsSource=factors;await window.ShowDialog(owner);
    }
}
