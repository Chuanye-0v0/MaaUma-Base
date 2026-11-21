using System;
using System.Collections.Generic;
using System.Globalization;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Ordinary controls for the existing targets and base attribute_weights contract.</summary>
public sealed class UmaAttributeGoalEditor : StackPanel
{
    private static readonly (string Key,string Label,int Goal)[] Attributes={
        ("speed","速度",1550),("stamina","耐力",1200),("power","力量",1200),("guts","毅力",1200),("wisdom","智力",1200)};
    private readonly Func<JObject?> readTargets,readPolicy;
    private readonly Action<JObject,JObject> applyRow;
    private readonly Action<JToken?> applyCredit;
    private readonly Func<string?>? readPriority;
    private readonly TextBlock status=Note("");
    private readonly TextBlock priority=Note("");
    private readonly List<Func<bool>> pendingCommits=new();
    private readonly List<Action> contextRefresh=new();
    public event Action? PendingChanged;
    private JObject? Weights()=>readPolicy()?["attribute_weights"] as JObject;
    private double DefaultWeight(string key)=>readPolicy() is JObject policy && (string?)policy["variant"]=="baseline"
        ? StrategyPackCatalog.DefaultWeight(key, (string?)policy["revision"])
        : 1;
    private double DefaultCredit()=>readPolicy() is JObject policy
        && (string?)policy["variant"]=="baseline" ? UmaPacerView.DefaultAfterTargetCreditForRevision((string?)policy["revision"]) : .5;

    public UmaAttributeGoalEditor(Func<JObject?> targets,Func<JObject?> policy,Action<JObject,JObject> update,
        Action<JToken?> updateCredit,string prefix,Func<string?>? priorityNotice=null,bool compactCards=false)
    {
        Name=prefix+"AttributeGoals";Spacing=10;readTargets=targets;readPolicy=policy;applyRow=update;applyCredit=updateCredit;readPriority=priorityNotice;
        var table=new StackPanel{Spacing=6,MinWidth=690};
        var heading=RowGrid();
        var labels=new[]{"属性","目标","基础属性偏好 ×","快捷填写",""};
        for(var i=0;i<labels.Length;i++){var label=Note(labels[i]);Grid.SetColumn(label,i);heading.Children.Add(label);}table.Children.Add(heading);
        var cardPanel=compactCards?new Grid{ColumnDefinitions=new("*,*,*,*,*"),ColumnSpacing=10,RowSpacing=10}:null;
        for(var idx=0;idx<Attributes.Length;idx++)
        {
            var attribute=Attributes[idx];
            TextBox? cardBox=null;TextBlock? cardBadge=null;
            if(compactCards&&cardPanel!=null)
            {
                cardBadge=new TextBlock{FontSize=11,VerticalAlignment=VerticalAlignment.Center};
                cardBadge.Bind(TextBlock.ForegroundProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiLowText"));
                cardBox=new TextBox{
                    Name=prefix+"CardTarget_"+attribute.Key,
                    Height=36,
                    MinHeight=0,
                    FontSize=15,
                    FontWeight=FontWeight.SemiBold,
                    FontFamily=new FontFamily("Cascadia Mono, Consolas"),
                    TextAlignment=TextAlignment.Center,
                    VerticalContentAlignment=VerticalAlignment.Center
                };
                ToolTip.SetTip(cardBox,"属性目标 0—3000；空白恢复默认目标，失焦即写入草稿。");
                var cardHeader=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=4};
                cardHeader.Children.Add(new TextBlock{Text=attribute.Label+"目标",FontSize=13,FontWeight=FontWeight.SemiBold,VerticalAlignment=VerticalAlignment.Center});
                Grid.SetColumn(cardBadge,1);cardHeader.Children.Add(cardBadge);
                var card=new Border{
                    CornerRadius=new CornerRadius(10),
                    BorderThickness=new Thickness(1),
                    Padding=new Thickness(10,8),
                    Child=new StackPanel{Spacing=6,Children={cardHeader,cardBox}}
                };
                card.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
                Grid.SetColumn(card,idx);
                cardPanel.Children.Add(card);
            }
            table.Children.Add(BuildRow(attribute.Key,attribute.Label,attribute.Goal,prefix,cardBox,cardBadge));
        }
        if(compactCards&&cardPanel!=null)
        {
            bool? lastCardNarrow=null;
            cardPanel.SizeChanged+=(_,_)=>{
                var w=cardPanel.Bounds.Width;
                if(w<=0)return;
                var narrow=PacerLayoutGuard.NextNarrow(w,560,lastCardNarrow,24);
                if(lastCardNarrow==narrow)return;
                lastCardNarrow=narrow;
                cardPanel.ColumnDefinitions=new(narrow?"*,*":"*,*,*,*,*");
                cardPanel.RowDefinitions=new(narrow?"Auto,Auto,Auto":"Auto");
                for(var i=0;i<cardPanel.Children.Count;i++)
                {
                    var child=cardPanel.Children[i];
                    Grid.SetColumn(child,narrow?(i%2):i);
                    Grid.SetRow(child,narrow?(i/2):0);
                }
            };
            var quickRow=new WrapPanel{Orientation=Orientation.Horizontal};
            void ApplyQuickTargets(int spd,int sta,int pow,int gut,int wis,string msg)
            {
                var nextTargets=new JObject{["speed"]=spd,["stamina"]=sta,["power"]=pow,["guts"]=gut,["wisdom"]=wis};
                try{applyRow(nextTargets,new JObject());RefreshContext();status.Text=msg;}
                catch(Exception err){status.Text=err.Message;}
            }
            var qStd=Button(prefix+"QuickStdGoals","标准 1550/1150",()=>ApplyQuickTargets(1550,1150,1200,1000,1200,"已填入标准目标 1550/1150，请点顶栏「保存配置」。"));
            var qHigh=Button(prefix+"QuickHighGoals","三速冲上限 1650/1200",()=>ApplyQuickTargets(1650,1200,1200,1000,1200,"已填入三速冲上限目标 1650/1200，请点顶栏「保存配置」。"));
            var qReset=Button(prefix+"ResetAllTargets","恢复默认目标",()=>{
                var clr=new JObject();
                foreach(var a in Attributes)AddDelta(clr,a.Key,readTargets()?[a.Key],null);
                try{if(clr.Count>0)applyRow(clr,new JObject());RefreshContext();status.Text="五维目标已恢复策略默认，请点顶栏「保存配置」。";}
                catch(Exception err){status.Text=err.Message;}
            });
            foreach(var b in new[]{qStd,qHigh,qReset}){b.FontSize=12;b.Height=28;b.Padding=new Thickness(10,0);quickRow.Children.Add(b);}
            var headLeft=new StackPanel{
                Spacing=2,
                Children={
                    new TextBlock{Text="五维培养目标（本局期望到达数值）",FontSize=16,FontWeight=FontWeight.SemiBold},
                    Note("✓ 仅修改五维培养目标仍属于官方纯净策略；输入失焦自动写入草稿。")
                }
            };
            var topGrid=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=12};
            topGrid.Children.Add(headLeft);
            Grid.SetColumn(quickRow,1);topGrid.Children.Add(quickRow);
            bool? lastTopNarrow=null;
            topGrid.SizeChanged+=(_,_)=>{
                var w=topGrid.Bounds.Width;
                if(w<=0)return;
                var narrow=PacerLayoutGuard.NextNarrow(w,640,lastTopNarrow,24);
                if(lastTopNarrow==narrow)return;
                lastTopNarrow=narrow;
                topGrid.ColumnDefinitions=new(narrow?"*":"*,Auto");
                topGrid.RowDefinitions=new(narrow?"Auto,Auto":"Auto");
                Grid.SetColumn(quickRow,narrow?0:1);Grid.SetRow(quickRow,narrow?1:0);
            };
            Children.Add(topGrid);
            Children.Add(cardPanel);
            priority.Name=prefix+"AttributePriorityNotice";RefreshPriority();Children.Add(priority);
            var advBody=new StackPanel{
                Spacing=8,
                Children={
                    Note("仅当你需要手动偏移策略五维评分偏好或修改达标后保留比例时展开此处；一般保持空默认跟随所选策略即可。"),
                    new ScrollViewer{Content=table,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,VerticalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled},
                    BuildCredit(prefix)
                }
            };
            Children.Add(new Expander{
                Name=prefix+"AdvancedAttributeWeights",
                Header="高级：自定义基础属性偏好 × 与达标后保留比例（99% 场景无需调整）",
                Content=advBody,
                IsExpanded=false,
                HorizontalContentAlignment=HorizontalAlignment.Stretch
            });
            status.Name=prefix+"AttributeGoalStatus";Children.Add(status);
        }
        else
        {
            Children.Add(new TextBlock{Text="五维目标与基础属性偏好",FontSize=18,FontWeight=FontWeight.SemiBold});
            Children.Add(Note("目标用来调整收益取舍；偏好作用于每点属性收益，不是整项训练倍率。"));
            Children.Add(new ScrollViewer{Content=table,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,VerticalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled});
            priority.Name=prefix+"AttributePriorityNotice";RefreshPriority();Children.Add(priority);
            Children.Add(BuildCredit(prefix));status.Name=prefix+"AttributeGoalStatus";Children.Add(status);
        }
    }

    private Control BuildRow(string key,string label,int defaultGoal,string prefix,TextBox? cardTargetBox=null,TextBlock? cardWeightBadge=null)
    {
        var grid=RowGrid();grid.Margin=new Thickness(0,0,0,3);
        var source=Note("");source.IsVisible=false;
        var name=new StackPanel{Spacing=2,VerticalAlignment=VerticalAlignment.Center,Children={new TextBlock{Text=label,FontSize=15,FontWeight=FontWeight.SemiBold},source}};grid.Children.Add(name);
        var target=Field(prefix+"Target_"+key);var weight=Field(prefix+"AttributeWeight_"+key);
        ToolTip.SetTip(target,"属性目标0—3000；0不表示禁止该类训练。空白恢复当前策略默认目标。");
        ToolTip.SetTip(weight,"基础属性收益偏好，大于0且不超过10；空白恢复所选策略的默认值。分段同项覆盖时以分段为准。");
        Grid.SetColumn(target,1);grid.Children.Add(target);Grid.SetColumn(weight,2);grid.Children.Add(weight);
        var presets=new ComboBox{Name=prefix+"AttributePreset_"+key,Height=34,MinHeight=0,FontSize=14,ItemsSource=new[]{"自填","低 · 0.8","标准 · 1","高 · 1.2"},SelectedIndex=0};
        Grid.SetColumn(presets,3);grid.Children.Add(presets);
        var buttons=new WrapPanel();Grid.SetColumn(buttons,4);grid.Children.Add(buttons);
        var loading=false;var targetDirty=false;var weightDirty=false;
        void Reload(){loading=true;target.ClearValue(Border.BorderBrushProperty);weight.ClearValue(Border.BorderBrushProperty);var tText=NumberText(readTargets()?[key],defaultGoal);var wToken=Weights()?[key];var defW=DefaultWeight(key);var wText=NumberText(wToken,defW);target.Text=tText;weight.Text=wText;if(cardTargetBox!=null){cardTargetBox.ClearValue(Border.BorderBrushProperty);cardTargetBox.Text=tText;}if(cardWeightBadge!=null){var hasCustom=!Unset(wToken);cardWeightBadge.Text=hasCustom?$"指定 {wText}":$"跟随 {defW.ToString("0.###",CultureInfo.InvariantCulture)}";cardWeightBadge.ClearValue(TextBlock.ForegroundProperty);if(hasCustom)cardWeightBadge.Foreground=new SolidColorBrush(Color.Parse("#D97706"));else cardWeightBadge.Bind(TextBlock.ForegroundProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiLowText"));}source.Text=(Unset(readTargets()?[key])?"目标默认":"目标已设")+" · "+(Unset(wToken)?"偏好默认":"偏好已设");ToolTip.SetTip(name,source.Text);ToolTip.SetTip(target,source.Text+"\n属性目标0—3000；0不表示禁止训练。空白恢复策略默认目标。");ToolTip.SetTip(weight,source.Text+"\n每点属性收益的基础偏好，大于0且不超过10；空白恢复所选策略的默认值。");presets.SelectedIndex=0;targetDirty=weightDirty=false;loading=false;}
        contextRefresh.Add(()=>{if(!targetDirty&&!weightDirty)Reload();});
        Reload();
        target.PropertyChanged+=(_,e)=>{if(!loading&&e.Property==TextBox.TextProperty){targetDirty=true;if(cardTargetBox!=null&&cardTargetBox.Text!=target.Text){loading=true;cardTargetBox.Text=target.Text;loading=false;}PendingChanged?.Invoke();}};
        if(cardTargetBox!=null)
            cardTargetBox.PropertyChanged+=(_,e)=>{if(!loading&&e.Property==TextBox.TextProperty){targetDirty=true;if(target.Text!=cardTargetBox.Text){loading=true;target.Text=cardTargetBox.Text;loading=false;}PendingChanged?.Invoke();}};
        weight.PropertyChanged+=(_,e)=>{if(!loading&&e.Property==TextBox.TextProperty){weightDirty=true;PendingChanged?.Invoke();}};
        bool CommitRow(){
            if(!targetDirty&&!weightDirty)return true;
            var targets=new JObject();var weights=new JObject();
            if(targetDirty&&!TryTarget(target.Text,out _)){status.Text=label+"目标请填0—3000的整数。";target.BorderBrush=Brushes.IndianRed;if(cardTargetBox!=null)cardTargetBox.BorderBrush=Brushes.IndianRed;return false;}
            if(weightDirty&&!TryWeight(weight.Text,out _)){status.Text=label+"偏好请填大于0、不超过10的数值。";weight.BorderBrush=Brushes.IndianRed;return false;}
            target.ClearValue(Border.BorderBrushProperty);weight.ClearValue(Border.BorderBrushProperty);if(cardTargetBox!=null)cardTargetBox.ClearValue(Border.BorderBrushProperty);
            if(targetDirty){TryTarget(target.Text,out var value);AddDelta(targets,key,readTargets()?[key],value);}
            if(weightDirty){TryWeight(weight.Text,out var value);AddDelta(weights,key,Weights()?[key],value);}
            var changed=targets.Count>0||weights.Count>0;
            try{if(changed)applyRow(targets,weights);}catch(Exception error){status.Text=error.Message;return false;}
            Reload();RefreshPriority();status.Text=changed?label+"已写入当前配置草稿；顶栏点“保存配置”提交。":label+"没有需要修改的内容。";return true;
        }
        presets.SelectionChanged+=(_,_)=>{if(loading||presets.SelectedIndex<=0)return;var value=new[]{"","0.8","1","1.2"}[presets.SelectedIndex];weight.Text=value;weightDirty=true;CommitRow();};
        target.LostFocus+=(_,_)=>CommitRow();weight.LostFocus+=(_,_)=>CommitRow();
        if(cardTargetBox!=null)cardTargetBox.LostFocus+=(_,_)=>CommitRow();
        pendingCommits.Add(CommitRow);var applyHidden=Button(prefix+"ApplyAttribute_"+key,"应用",()=>CommitRow());applyHidden.IsVisible=false;buttons.Children.Add(applyHidden);
        buttons.Children.Add(Button(prefix+"ResetAttribute_"+key,"↺ 默认",()=>{var targets=new JObject();var weights=new JObject();AddDelta(targets,key,readTargets()?[key],null);AddDelta(weights,key,Weights()?[key],null);try{if(targets.Count>0||weights.Count>0)applyRow(targets,weights);}catch(Exception error){status.Text=error.Message;return;}Reload();RefreshPriority();status.Text=label+"恢复策略默认；请点“保存配置”。";}));
        return grid;
    }

    private Control BuildCredit(string prefix)
    {
        var body=new StackPanel{Spacing=7};var description=Note("");description.Name=prefix+"AfterTargetCreditNotice";body.Children.Add(description);
        var field=Field(prefix+"AfterTargetCredit");field.Width=130;
        var row=new WrapPanel();row.Children.Add(field);var dirty=false;var loadingCredit=false;
        Button reset=null!;
        void RefreshCreditLabels(){
            var defaultText=DefaultCredit().ToString("0.###",CultureInfo.InvariantCulture);
            description.Text="达标后的收益保留比例由五项属性共用；当前策略默认"+defaultText+"。显式自定义值优先，旧目标和正在进行的育成不自动修改。";
            ToolTip.SetTip(field,"大于0、不超过1；空白恢复所选策略默认"+defaultText+"。已保存或明确填写的自定义值优先。");
            if(reset!=null)reset.Content="默认"+defaultText;
        }
        field.PropertyChanged+=(_,e)=>{if(!loadingCredit&&e.Property==TextBox.TextProperty){dirty=true;PendingChanged?.Invoke();}};
        void ReloadCredit(){loadingCredit=true;field.ClearValue(Border.BorderBrushProperty);field.Text=NumberText(readPolicy()?["after_target_credit"],DefaultCredit());dirty=false;loadingCredit=false;RefreshCreditLabels();}
        bool CommitCredit(){
            if(!dirty)return true;JToken? value=null;var text=field.Text?.Trim();
            if(!string.IsNullOrEmpty(text)){if(!double.TryParse(text,NumberStyles.Float,CultureInfo.InvariantCulture,out var number)||!double.IsFinite(number)||number<=0||number>1){status.Text="共用保留比例请填大于0、不超过1的数值。";field.BorderBrush=Brushes.IndianRed;return false;}value=new JValue(number);}
            field.ClearValue(Border.BorderBrushProperty);
            try{if(!Same(readPolicy()?["after_target_credit"],value))applyCredit(value);}catch(Exception error){status.Text=error.Message;return false;}
            ReloadCredit();status.Text="已写入五项共用保留比例到草稿；顶栏点“保存配置”提交。";return true;
        }
        field.LostFocus+=(_,_)=>CommitCredit();
        pendingCommits.Add(CommitCredit);
        var applyCreditHidden=Button(prefix+"ApplyAfterTargetCredit","应用共用比例",()=>CommitCredit());applyCreditHidden.IsVisible=false;
        row.Children.Add(applyCreditHidden);
        reset=Button(prefix+"ResetAfterTargetCredit","↺ 默认",()=>{try{if(readPolicy()?["after_target_credit"]!=null)applyCredit(null);}catch(Exception error){status.Text=error.Message;return;}ReloadCredit();status.Text="五项共用比例恢复策略默认；请点击“保存配置”。";});row.Children.Add(reset);
        contextRefresh.Add(()=>{RefreshCreditLabels();if(!dirty)ReloadCredit();});ReloadCredit();
        body.Children.Add(row);return new Expander{Name=prefix+"SharedAfterTargetCredit",Header="更多 · 达标后的取舍（五项共用）",Content=body,IsExpanded=false,HorizontalContentAlignment=HorizontalAlignment.Stretch};
    }

    private void RefreshPriority(){priority.Text=readPriority?.Invoke()??"";priority.IsVisible=!string.IsNullOrEmpty(priority.Text);}
    public void RefreshContext(){RefreshPriority();foreach(var refresh in contextRefresh)refresh();}
    public bool TryCommitPending(){foreach(var commit in pendingCommits)if(!commit())return false;return true;}
    private static Grid RowGrid()=>new(){ColumnDefinitions=new("100,110,135,150,*"),ColumnSpacing=10};
    private static TextBlock Note(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=12};
    private static TextBox Field(string name)=>new(){Name=name,Height=34,MinHeight=0,FontSize=14,VerticalContentAlignment=VerticalAlignment.Center};
    private static Button Button(string name,string text,Action action){var result=new Button{Name=name,Content=text,Height=32,MinHeight=0,FontSize=14,Padding=new Thickness(10,5),Margin=new Thickness(4,0,0,0)};result.Click+=(_,_)=>action();return result;}
    private static bool Unset(JToken? value)=>value==null||value.Type==JTokenType.Null;
    private static string NumberText(JToken? value,double fallback)=>Unset(value)?fallback.ToString("0.###",CultureInfo.InvariantCulture):value!.ToString();
    private static bool TryTarget(string? text,out JToken? token){token=null;if(string.IsNullOrWhiteSpace(text))return true;if(!int.TryParse(text,NumberStyles.Integer,CultureInfo.InvariantCulture,out var value)||value<0||value>3000)return false;token=new JValue(value);return true;}
    private static bool TryWeight(string? text,out JToken? token){token=null;if(string.IsNullOrWhiteSpace(text))return true;if(!double.TryParse(text,NumberStyles.Float,CultureInfo.InvariantCulture,out var value)||!double.IsFinite(value)||value<=0||value>10)return false;token=new JValue(value);return true;}
    private static bool Same(JToken? left,JToken? right)=>Unset(left)&&Unset(right)||JToken.DeepEquals(left,right);
    private static void AddDelta(JObject changes,string key,JToken? current,JToken? value){if(!Same(current,value))changes[key]=value?.DeepClone()??JValue.CreateNull();}
}
