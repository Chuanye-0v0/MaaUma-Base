using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using MFAAvalonia.Helper;

namespace UmaCutPanel;

internal sealed class CutPanelLayout:Grid
{
    public TabControl Sections {get;}

    public CutPanelLayout(Control connection,Control status,Control runtime,EventPreferenceEditor preferences,Control? review=null,Control? updates=null,Control? exchange=null)
    {
        RowDefinitions=new("Auto,Auto,*");
        Children.Add(connection);
        Grid.SetRow(status,1);Children.Add(status);
        preferences.IsExpanded=true;
        var pages=new List<TabItem> {new TabItem {Header="运行面板",Content=runtime}};
        var sectionRow=2;
        var eventEditors=new StackPanel {Spacing=22,Children={preferences}};
        if(runtime is UmaPacerView {SupportsSettingsPages:true} page) {
            var settings=page.SeparateSettingsPages();
            RowDefinitions=new("Auto,Auto,Auto,*");
            Grid.SetRow(settings.Presets,2);Children.Add(settings.Presets);sectionRow=3;
            eventEditors.Children.Add(settings.MidSkills);
            pages.Add(SettingsTab("继续育成",settings.Continuation,"CutContinuationSettingsScroll"));
            pages.Add(SettingsTab("自定义权重",settings.Weights,"CutWeightsSettingsScroll"));
            Loaded+=(_,_)=>page.SetSettingsHostVisible(true);
            Unloaded+=(_,_)=>page.SetSettingsHostVisible(false);
        }
        pages.Add(new TabItem {Header="事件设置",Content=new ScrollViewer {
                Name="EventSettingsScroll",Content=eventEditors,
                HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled,
                VerticalScrollBarVisibility=ScrollBarVisibility.Auto}});
        if(review!=null)pages.Add(new TabItem {Header="复盘导出",Content=review});
        if(exchange!=null)pages.Add(new TabItem {Header="权重交换站",Content=exchange});
        if(updates!=null)pages.Add(new TabItem {Header="更新与服务",Content=updates});
        Sections=new TabControl {
            Name="CutPanelSections",SelectedIndex=0,
            HorizontalContentAlignment=HorizontalAlignment.Stretch,
            VerticalContentAlignment=VerticalAlignment.Stretch,
            ItemsPanel=new FuncTemplate<Panel?>(()=>new WrapPanel()),
            ItemsSource=pages
        };
        if(runtime is UmaPacerView editor)editor.MidSkillSettingsRequested+=()=>
            Sections.SelectedItem=pages.First(page=>(string?)page.Header=="事件设置");
        Grid.SetRow(Sections,sectionRow);Children.Add(Sections);
    }

    private static TabItem SettingsTab(string title,Control editor,string name)
    {
        var tab=new TabItem {Header=title,Content=new ScrollViewer {Name=name,
            HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled,VerticalScrollBarVisibility=ScrollBarVisibility.Auto,
            HorizontalContentAlignment=HorizontalAlignment.Stretch,VerticalContentAlignment=VerticalAlignment.Top,
            Content=new Border {Child=editor,Padding=new Thickness(24),Margin=new Thickness(12),
                MaxWidth=900,HorizontalAlignment=HorizontalAlignment.Stretch,VerticalAlignment=VerticalAlignment.Top}}};
        tab.Bind(IsVisibleProperty,editor.GetObservable(IsVisibleProperty));
        return tab;
    }

    public static Grid ConnectionBar(TextBox adb,TextBox port,Button choose,Button save,Button probe)
    {
        var row=new Grid {ColumnDefinitions=new("Auto,*,Auto,Auto,Auto,Auto,Auto"),
            ColumnSpacing=8,Margin=new Thickness(12)};
        Control[] fields={new TextBlock {Text="ADB",VerticalAlignment=VerticalAlignment.Center},adb,choose,
            new TextBlock {Text="端口",VerticalAlignment=VerticalAlignment.Center},port,save,probe};
        for(var index=0;index<fields.Length;index++){
            Grid.SetColumn(fields[index],index);row.Children.Add(fields[index]);
        }
        return row;
    }
}
