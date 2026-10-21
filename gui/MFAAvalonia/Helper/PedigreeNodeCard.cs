using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using MFAAvalonia.Views.UserControls;

namespace MFAAvalonia.Helper;

/// <summary>Short identity/status labels; the shared inspector owns long details.</summary>
public sealed class PedigreeNodeCard : Button
{
    private readonly Border frame=new(){Padding=new Thickness(7),CornerRadius=new CornerRadius(10),BorderThickness=new Thickness(1)};
    private readonly TextBlock title=new(){FontSize=11,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly TextBlock name=new(){FontSize=14,FontWeight=FontWeight.SemiBold,MaxLines=2,TextWrapping=TextWrapping.Wrap,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly TextBlock outfit=new(){FontSize=11,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly TextBlock status=new(){FontSize=11,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly TextBlock red=new(){FontSize=11,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly DisplayIcon portrait=new(){IconSource="?",IconSize=44,Width=44,Height=44,VerticalAlignment=VerticalAlignment.Center};
    public string NodeKey {get;}

    public PedigreeNodeCard(string key,string caption)
    {
        NodeKey=key;Name="PedigreeNode_"+key;Padding=new Thickness(0);HorizontalContentAlignment=HorizontalAlignment.Stretch;
        VerticalContentAlignment=VerticalAlignment.Stretch;HorizontalAlignment=HorizontalAlignment.Stretch;MinHeight=100;
        title.Text=key switch{"gp_a1"=>"祖辈 AA","gp_a2"=>"祖辈 AB","gp_b1"=>"祖辈 BA","gp_b2"=>"祖辈 BB",_=>caption};
        title.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
        outfit.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
        foreach(var label in new[]{name,status,red})label.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        var root=new StackPanel{Spacing=3};root.Children.Add(title);
        var identity=new Grid{ColumnDefinitions=new("44,*"),ColumnSpacing=7,MinHeight=44};identity.Children.Add(portrait);
        var labels=new StackPanel{Spacing=2,VerticalAlignment=VerticalAlignment.Center};labels.Children.Add(name);labels.Children.Add(outfit);
        Grid.SetColumn(labels,1);identity.Children.Add(labels);root.Children.Add(identity);
        var footer=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=5};footer.Children.Add(status);Grid.SetColumn(red,1);footer.Children.Add(red);
        root.Children.Add(footer);frame.Child=root;
        frame.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        frame.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));Content=frame;
        SetIdentity("未选择","","");SetState("待选择");SetRed(null,true);
    }

    public void SetIdentity(string character,string clothing,string portraitPath)
    {
        name.Text=string.IsNullOrWhiteSpace(character)?"资料未齐":character;
        outfit.Text=clothing;outfit.IsVisible=!string.IsNullOrWhiteSpace(clothing);
        portrait.IconSource=string.IsNullOrWhiteSpace(portraitPath)?"?":portraitPath;
        ToolTip.SetTip(name,name.Text);ToolTip.SetTip(outfit,clothing);
    }

    public void SetState(string value,bool locked=false)
    {
        var full=value+(locked?" · 随亲代锁定":"");
        // A compact lock label changes presentation only. The supplied state
        // remains available in its tooltip and the single shared inspector.
        var label=value switch{"本账号自有"=>"已有","已明确可借"=>"可借","随可用亲代继承"=>"随亲代", "亲代参考快照"=>"参考快照",
            "待培养（方案）"=>"待培养","本次育成目标"=>"本次目标","手动参考（未确认持有）"=>"仅作参考",_=>value};
        if(value.StartsWith("随成品亲代展开",StringComparison.Ordinal))label="快照待核";
        else if(value.StartsWith("快照资料未齐",StringComparison.Ordinal))label="待核对";
        status.Text=label+(locked?" · 锁":"");ToolTip.SetTip(status,full);
    }

    public void SetRed(int? known,bool unknown,bool conflict=false,string? planned=null)
    {
        var full=planned??(known is int n?$"已确认红星 {n}"+(unknown?" · 仍有未知":""):"红星资料未知");
        if(conflict)full+=" · 记录冲突";
        red.Text=planned??(conflict?"红星冲突":known is int stars?$"红{stars}"+(unknown?"·未齐":"星") :"红星未知");
        ToolTip.SetTip(red,full);
    }

    public void Select(bool selected)
    {
        frame.BorderThickness=new Thickness(selected?2:1);
        frame.Padding=new Thickness(selected?6:7);
        frame.Bind(Border.BorderBrushProperty,new DynamicResourceExtension(selected?"SukiPrimaryColor":"SukiControlBorderBrush"));
    }
}
