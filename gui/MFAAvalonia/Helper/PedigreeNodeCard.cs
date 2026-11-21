using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using MFAAvalonia.Views.UserControls;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Node card with portrait, affinity contribution, 10-aptitude grid and prominent red bar.</summary>
public sealed class PedigreeNodeCard : Button
{
    private sealed class AptCell
    {
        public readonly string Key;
        public readonly Border Box;
        public readonly TextBlock Tag;
        public readonly TextBlock Rank;
        public AptCell(string key,string label)
        {
            Key=key;
            Tag=new TextBlock{Text=label,FontSize=9,HorizontalAlignment=HorizontalAlignment.Center};
            Tag.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
            Rank=new TextBlock{Text="-",FontSize=11,FontWeight=FontWeight.Bold,HorizontalAlignment=HorizontalAlignment.Center};
            Box=new Border
            {
                Padding=new Thickness(2,1),
                CornerRadius=new CornerRadius(4),
                BorderThickness=new Thickness(1),
                Child=new StackPanel{Spacing=0,Children={Tag,Rank}}
            };
            ResetDefault();
        }
        public void ResetDefault()
        {
            Box.Background=new SolidColorBrush(Color.FromArgb(16,120,130,150));
            Box.BorderBrush=new SolidColorBrush(Color.FromArgb(40,120,130,150));
            Rank.Text="-";
            Rank.Foreground=Brushes.SlateGray;
            ToolTip.SetTip(Box,null);
        }
    }

    private readonly Border frame=new(){Padding=new Thickness(7),CornerRadius=new CornerRadius(10),BorderThickness=new Thickness(1)};
    private readonly TextBlock title=new(){FontSize=11,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly Border affinityPill=new(){Padding=new Thickness(5,1),CornerRadius=new CornerRadius(8),Background=new SolidColorBrush(Color.FromArgb(48,109,40,217)),BorderThickness=new Thickness(1),BorderBrush=new SolidColorBrush(Color.FromArgb(120,139,92,246)),IsVisible=false};
    private readonly TextBlock affinityText=new(){FontSize=10,FontWeight=FontWeight.SemiBold};
    private readonly TextBlock name=new(){FontSize=13,FontWeight=FontWeight.SemiBold,MaxLines=2,TextWrapping=TextWrapping.Wrap,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly TextBlock outfit=new(){FontSize=10,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly TextBlock status=new(){FontSize=10,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly Border subBadgeBox=new(){Padding=new Thickness(5,2),CornerRadius=new CornerRadius(6),Background=new SolidColorBrush(Color.FromArgb(28,217,119,6)),BorderThickness=new Thickness(1),BorderBrush=new SolidColorBrush(Color.FromArgb(90,217,119,6)),IsVisible=false};
    private readonly TextBlock subBadge=new(){FontSize=10,FontWeight=FontWeight.SemiBold,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly Grid aptGridContainer=new(){ColumnDefinitions=new("*,*,*,*"),RowDefinitions=new("Auto,Auto,Auto"),ColumnSpacing=3,RowSpacing=3,IsVisible=false,Margin=new Thickness(0,2,0,1)};
    private readonly AptCell[] aptCells;
    private readonly Border redBar=new(){Padding=new Thickness(6,3),CornerRadius=new CornerRadius(6),BorderThickness=new Thickness(1),HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly TextBlock red=new(){FontSize=11,FontWeight=FontWeight.SemiBold,HorizontalAlignment=HorizontalAlignment.Center,MaxLines=1,TextTrimming=TextTrimming.CharacterEllipsis};
    private readonly DisplayIcon portrait=new(){IconSource="?",IconSize=40,Width=40,Height=40,VerticalAlignment=VerticalAlignment.Center};
    public string NodeKey {get;}

    public PedigreeNodeCard(string key,string caption)
    {
        NodeKey=key;Name="PedigreeNode_"+key;Padding=new Thickness(0);HorizontalContentAlignment=HorizontalAlignment.Stretch;
        VerticalContentAlignment=VerticalAlignment.Stretch;HorizontalAlignment=HorizontalAlignment.Stretch;MinHeight=104;
        title.Text=key switch{"gp_a1"=>"祖辈 AA","gp_a2"=>"祖辈 AB","gp_b1"=>"祖辈 BA","gp_b2"=>"祖辈 BB",_=>caption};
        title.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
        outfit.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
        foreach(var label in new[]{name,status,red,affinityText,subBadge})label.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        affinityPill.Child=affinityText;
        subBadgeBox.Child=subBadge;

        var root=new StackPanel{Spacing=3};
        var header=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=4};
        header.Children.Add(title);
        Grid.SetColumn(affinityPill,1);header.Children.Add(affinityPill);
        root.Children.Add(header);

        var identity=new Grid{ColumnDefinitions=new("40,*"),ColumnSpacing=7,MinHeight=40};identity.Children.Add(portrait);
        var labels=new StackPanel{Spacing=1,VerticalAlignment=VerticalAlignment.Center};labels.Children.Add(name);labels.Children.Add(outfit);labels.Children.Add(status);
        Grid.SetColumn(labels,1);identity.Children.Add(labels);root.Children.Add(identity);

        aptCells=
        [
            new("red:草地","草"), new("red:泥地","泥"),
            new("red:短距离","短"), new("red:英里","英"), new("red:中距离","中"), new("red:长距离","长"),
            new("red:领跑","逃"), new("red:跟前","先"), new("red:居中","差"), new("red:后追","追"),
        ];
        // Layout 10-aptitude grid: row0 col2..3 = turf/dirt, row1 = distances, row2 = styles
        PlaceCell(aptCells[0].Box,0,2); PlaceCell(aptCells[1].Box,0,3);
        for(var i=0;i<4;i++) PlaceCell(aptCells[2+i].Box,1,i);
        for(var i=0;i<4;i++) PlaceCell(aptCells[6+i].Box,2,i);
        root.Children.Add(aptGridContainer);
        root.Children.Add(subBadgeBox);

        redBar.Child=red;
        redBar.Background=new SolidColorBrush(Color.FromArgb(24,59,130,246));
        redBar.BorderBrush=new SolidColorBrush(Color.FromArgb(80,59,130,246));
        root.Children.Add(redBar);

        frame.Child=root;
        frame.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        frame.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));Content=frame;
        SetIdentity("未选择","","");SetState("待选择");SetRed(null,true);
    }

    private void PlaceCell(Control ctrl,int row,int col)
    {
        Grid.SetRow(ctrl,row);Grid.SetColumn(ctrl,col);aptGridContainer.Children.Add(ctrl);
    }

    private static IBrush RankBrush(string rank)=>rank switch
    {
        "S"=>new SolidColorBrush(Color.Parse("#D97706")),
        "A"=>new SolidColorBrush(Color.Parse("#EA580C")),
        "B"=>new SolidColorBrush(Color.Parse("#DB2777")),
        "C"=>new SolidColorBrush(Color.Parse("#16A34A")),
        "D"=>new SolidColorBrush(Color.Parse("#2563EB")),
        "E"=>new SolidColorBrush(Color.Parse("#7C3AED")),
        _=>Brushes.SlateGray,
    };

    public void SetAptitudeGrid(JArray? grid)
    {
        if(grid==null||grid.Count==0){aptGridContainer.IsVisible=false;return;}
        var anyKnown=false;
        foreach(var cell in aptCells)cell.ResetDefault();
        foreach(var tok in grid)
        {
            if(tok is not JObject row)continue;
            var fk=row["factor_key"]?.ToString()??"";
            var cell=Array.Find(aptCells,c=>c.Key==fk);
            if(cell==null)continue;
            var baseRank=row["baseline_rank"]?.ToString()??"";
            var effRank=row["effective_rank"]?.ToString()??"";
            var planRank=row["planned_rank"]?.ToString()??"";
            var displayRank=planRank.Length>0?planRank:(effRank.Length>0?effRank:baseRank);
            if(displayRank.Length==0)continue;
            anyKnown=true;
            cell.Rank.Text=displayRank;
            cell.Rank.Foreground=RankBrush(displayRank);
            var confStars=(int?)row["confirmed_stars"]??0;
            var planStars=(int?)row["planned_stars"]??0;
            var upgraded=(bool?)row["upgraded"]==true;
            var planUpgraded=(bool?)row["planned_upgraded"]==true;
            if(upgraded||planUpgraded)
            {
                cell.Box.Background=upgraded
                    ?new SolidColorBrush(Color.FromArgb(52,37,99,235))
                    :new SolidColorBrush(Color.FromArgb(32,59,130,246));
                cell.Box.BorderBrush=new SolidColorBrush(Color.Parse("#3B82F6"));
            }
            var lbl=row["label"]?.ToString()??cell.Tag.Text;
            var tip=$"{lbl}：先天 {(baseRank.Length>0?baseRank:"?")}";
            if(effRank.Length>0&&effRank!=baseRank)tip+=$" → 已确认 {effRank} (+{confStars}★)";
            if(planRank.Length>0&&planRank!=effRank)tip+=$" → 含计划 {planRank} (+{confStars+planStars}★)";
            else if(confStars+planStars>0)tip+=$"（继承红星 {confStars}+{planStars}★）";
            ToolTip.SetTip(cell.Box,tip);
        }
        aptGridContainer.IsVisible=anyKnown;
    }

    public void SetAffinityBadge(string? text)
    {
        var has=!string.IsNullOrWhiteSpace(text);
        affinityText.Text=text??"";
        affinityPill.IsVisible=has;
    }

    public void SetSubBadge(string? text)
    {
        var has=!string.IsNullOrWhiteSpace(text);
        subBadge.Text=text??"";
        subBadgeBox.IsVisible=has;
        ToolTip.SetTip(subBadgeBox,has?text:null);
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
        var label=value switch{"本账号自有"=>"已有","已明确可借"=>"可借","随可用亲代继承"=>"随亲代", "亲代参考快照"=>"参考快照",
            "待培养（方案）"=>"待培养","本次育成目标"=>"本次目标","手动参考（未确认持有）"=>"仅作参考",_=>value};
        if(value.StartsWith("随成品亲代展开",StringComparison.Ordinal))label="快照待核";
        else if(value.StartsWith("快照资料未齐",StringComparison.Ordinal))label="待核对";
        status.Text=label+(locked?" · 锁":"");ToolTip.SetTip(status,full);
    }

    public void SetRed(int? known,bool unknown,bool conflict=false,string? planned=null,string? factorLabel=null)
    {
        var hasFactor=!string.IsNullOrWhiteSpace(factorLabel);
        var full=planned??(known is int n?$"已确认{(hasFactor?factorLabel:"红")} {n} 星"+(unknown?" · 仍有未知":""):(hasFactor?$"红因子 {factorLabel} 未知":"红星资料未知"));
        if(conflict)full+=" · 记录冲突";
        var text=planned??(conflict?"红星冲突":known is int stars?(stars==0&&!unknown&&!hasFactor?"无红因子":$"{(hasFactor?factorLabel:"红")}{stars}"+(unknown?"·未齐":"★")):"红星未知");
        red.Text=text;
        ToolTip.SetTip(red,full);
        if(conflict||text.Contains('⚠'))
        {
            redBar.Background=new SolidColorBrush(Color.FromArgb(44,217,119,6));
            redBar.BorderBrush=new SolidColorBrush(Color.Parse("#D97706"));
        }
        else if(text.Contains('★')||text.StartsWith("计划 ")||text.StartsWith("追"))
        {
            redBar.Background=new SolidColorBrush(Color.FromArgb(46,37,99,235));
            redBar.BorderBrush=new SolidColorBrush(Color.Parse("#3B82F6"));
        }
        else
        {
            redBar.Background=new SolidColorBrush(Color.FromArgb(14,120,130,150));
            redBar.BorderBrush=new SolidColorBrush(Color.FromArgb(50,120,130,150));
        }
    }

    public void Select(bool selected)
    {
        frame.BorderThickness=new Thickness(selected?2:1);
        frame.Padding=new Thickness(selected?6:7);
        frame.Bind(Border.BorderBrushProperty,new DynamicResourceExtension(selected?"SukiPrimaryColor":"SukiControlBorderBrush"));
    }
}
