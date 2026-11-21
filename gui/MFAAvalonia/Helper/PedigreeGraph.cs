using System;
using System.Collections.Generic;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Visual 4/5-layer pedigree tree with affinity pills and Layer-4/Layer-5 ancestor strips.</summary>
public sealed class PedigreeGraph : Grid
{
    private const double CompactBreakpoint=760;
    private readonly Control[] cards;
    private readonly Grid[] branches;
    private readonly Grid[] ancestorPanels=new Grid[6];
    private readonly Grid[] layer4Rows=new Grid[6];
    private readonly Grid[] layer5Rows=new Grid[6];
    private readonly Button[,][] l4Buttons=new Button[6,2][];
    private readonly Button[,][] l5Buttons=new Button[6,4][];
    private readonly Links links;
    private readonly Border totalAffBadge=new(){Padding=new Thickness(8,3),CornerRadius=new CornerRadius(8),Background=new SolidColorBrush(Color.FromArgb(30,59,130,246)),BorderThickness=new Thickness(1),BorderBrush=new SolidColorBrush(Color.FromArgb(95,59,130,246)),IsVisible=false};
    private readonly TextBlock totalAffText=new(){FontSize=12,FontWeight=FontWeight.SemiBold};
    private readonly Border mutualAffBadge=new(){Padding=new Thickness(9,3),CornerRadius=new CornerRadius(10),Background=new SolidColorBrush(Color.FromArgb(200,109,40,217)),BorderThickness=new Thickness(1),BorderBrush=new SolidColorBrush(Color.FromArgb(240,139,92,246)),IsVisible=false,HorizontalAlignment=HorizontalAlignment.Center,VerticalAlignment=VerticalAlignment.Top,Margin=new Thickness(0,-11,0,0),IsHitTestVisible=false};
    private readonly TextBlock mutualAffText=new(){FontSize=11,FontWeight=FontWeight.Bold,Foreground=Brushes.White};
    private readonly Button toggleLayer5=new(){Name="PedigreeToggleLayer5",Content="展开第 5 层曾曾祖辈（算祖辈改泥/英）",FontSize=11,Padding=new Thickness(8,3)};
    private bool compact,arranged,showLayer5;
    public int NodeCount=>cards.Length;
    public int LinkCount=>6;
    public event Action<int>? AncestorSlotClicked;

    public PedigreeGraph(Control target,IReadOnlyList<Control> lineage)
    {
        if(lineage.Count!=6)throw new ArgumentException("Six lineage nodes required.");
        Name="ParentPedigreeGraph";MaxWidth=1080;HorizontalAlignment=HorizontalAlignment.Center;VerticalAlignment=VerticalAlignment.Top;
        ColumnDefinitions=new("*,*");RowDefinitions=new("Auto,Auto,Auto");ColumnSpacing=18;RowSpacing=14;
        cards=[target,lineage[0],lineage[1],lineage[2],lineage[3],lineage[4],lineage[5]];
        foreach(var card in cards){card.Width=double.NaN;card.Margin=new Thickness(0);card.HorizontalAlignment=HorizontalAlignment.Stretch;}

        totalAffText.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        totalAffBadge.Child=totalAffText;
        var headerBar=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=10,Margin=new Thickness(0,0,0,2)};
        var leftHeader=new WrapPanel{Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center};
        leftHeader.Children.Add(totalAffBadge);
        headerBar.Children.Add(leftHeader);
        Grid.SetColumn(toggleLayer5,1);headerBar.Children.Add(toggleLayer5);
        Grid.SetColumnSpan(headerBar,2);Grid.SetRow(headerBar,0);Children.Add(headerBar);

        target.Width=290;target.MaxWidth=290;target.HorizontalAlignment=HorizontalAlignment.Center;
        Grid.SetColumnSpan(target,2);Grid.SetRow(target,1);Children.Add(target);

        foreach(var gi in new[]{1,2,4,5}) BuildAncestorStrip(gi);

        branches=[Branch("A","亲代 A → 祖辈 AA / AB → 第4/5层",1),Branch("B","亲代 B → 祖辈 BA / BB → 第4/5层",4)];
        mutualAffBadge.Child=mutualAffText;
        Grid.SetColumnSpan(mutualAffBadge,2);Grid.SetRow(mutualAffBadge,2);Children.Add(mutualAffBadge);

        links=new Links(this){Name="PedigreeLocalLinks",IsHitTestVisible=false};
        Grid.SetColumnSpan(links,2);Grid.SetRow(links,1);Grid.SetRowSpan(links,2);Children.Add(links);

        toggleLayer5.Click+=(_,_)=>{showLayer5=!showLayer5;UpdateLayer5Visibility();};
        LayoutUpdated+=(_,_)=>links.InvalidateVisual();
        UpdateLayer5Visibility();
        SetCompact(false);
    }

    private void BuildAncestorStrip(int gpSlotIdx)
    {
        var host=new Grid{Name="PedigreeAncestors_"+gpSlotIdx,RowDefinitions=new("Auto,Auto"),RowSpacing=4,Margin=new Thickness(0,2,0,0)};
        var r4=new Grid{ColumnDefinitions=new("*,*"),ColumnSpacing=4};
        for(var j=0;j<2;j++)
        {
            var btn=MakeMiniButton("+",10,36,gpSlotIdx);
            l4Buttons[gpSlotIdx,j]=[btn];
            Grid.SetColumn(btn,j);r4.Children.Add(btn);
        }
        var r5=new Grid{ColumnDefinitions=new("*,*,*,*"),ColumnSpacing=3,IsVisible=false};
        for(var k=0;k<4;k++)
        {
            var btn=MakeMiniButton("+",9,26,gpSlotIdx);
            l5Buttons[gpSlotIdx,k]=[btn];
            Grid.SetColumn(btn,k);r5.Children.Add(btn);
        }
        Grid.SetRow(r4,0);host.Children.Add(r4);
        Grid.SetRow(r5,1);host.Children.Add(r5);
        layer4Rows[gpSlotIdx]=r4;
        layer5Rows[gpSlotIdx]=r5;
        ancestorPanels[gpSlotIdx]=host;
    }

    private Button MakeMiniButton(string text,double fontSize,double minHeight,int gpSlotIdx)
    {
        var label=new TextBlock{Text=text,FontSize=fontSize,HorizontalAlignment=HorizontalAlignment.Center,TextAlignment=TextAlignment.Center,MaxLines=2,TextWrapping=TextWrapping.Wrap,TextTrimming=TextTrimming.CharacterEllipsis};
        label.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        var border=new Border{Padding=new Thickness(3,2),CornerRadius=new CornerRadius(6),BorderThickness=new Thickness(1),Background=new SolidColorBrush(Color.FromArgb(14,120,130,150)),BorderBrush=new SolidColorBrush(Color.FromArgb(55,120,130,150)),Child=label};
        var btn=new Button{Padding=new Thickness(0),MinHeight=minHeight,HorizontalAlignment=HorizontalAlignment.Stretch,HorizontalContentAlignment=HorizontalAlignment.Stretch,VerticalContentAlignment=VerticalAlignment.Stretch,Content=border};
        btn.Click+=(_,_)=>AncestorSlotClicked?.Invoke(gpSlotIdx);
        return btn;
    }

    private void UpdateLayer5Visibility()
    {
        foreach(var gi in new[]{1,2,4,5})
            if(layer5Rows[gi]!=null)layer5Rows[gi].IsVisible=showLayer5;
        toggleLayer5.Content=showLayer5?"收起第 5 层曾曾祖辈":"展开第 5 层曾曾祖辈（算祖辈改泥/英）";
    }

    public void SetAffinityHeader(string? totalText,string? mutualText)
    {
        totalAffText.Text=totalText??"";
        totalAffBadge.IsVisible=!string.IsNullOrWhiteSpace(totalText);
        mutualAffText.Text=mutualText??"";
        mutualAffBadge.IsVisible=!string.IsNullOrWhiteSpace(mutualText);
    }

    public void SetAncestorData(int gpSlotIndex,JArray? layer4,JArray? layer5)
    {
        if(gpSlotIndex is not (1 or 2 or 4 or 5))return;
        for(var j=0;j<2;j++)
        {
            var btn=l4Buttons[gpSlotIndex,j][0];
            var border=(Border)btn.Content!;
            var tb=(TextBlock)border.Child!;
            var item=layer4?.Count>j?layer4[j] as JObject:null;
            var rf=item?["red_factor"] as JObject;
            var cname=item?["character_name"]?.ToString()??"";
            if(rf!=null)
            {
                var shortLbl=rf["short_label"]?.ToString()??rf["label"]?.ToString()??"";
                var stars=(int?)rf["stars"]??0;
                tb.Text=cname.Length>0?$"{cname}\n{shortLbl} {stars}★":$"{shortLbl} {stars}★";
                var isPlanned=item?["source"]?.ToString()=="planned_budget";
                border.Background=new SolidColorBrush(isPlanned?Color.FromArgb(32,59,130,246):Color.FromArgb(40,37,99,235));
                border.BorderBrush=new SolidColorBrush(Color.Parse("#3B82F6"));
                ToolTip.SetTip(btn,$"第 4 层（影响亲代开局适性）：{(cname.Length>0?cname+" · ":"")}{rf["label"]} {stars}★（点击编辑）");
            }
            else
            {
                tb.Text=cname.Length>0?$"{cname}\n未设":"+\n第4层";
                border.Background=new SolidColorBrush(Color.FromArgb(14,120,130,150));
                border.BorderBrush=new SolidColorBrush(Color.FromArgb(55,120,130,150));
                ToolTip.SetTip(btn,"第 4 层曾祖辈（用于抬亲代泥D/英B）：点击在右侧编辑");
            }
        }
        for(var k=0;k<4;k++)
        {
            var btn=l5Buttons[gpSlotIndex,k][0];
            var border=(Border)btn.Content!;
            var tb=(TextBlock)border.Child!;
            var item=layer5?.Count>k?layer5[k] as JObject:null;
            var rf=item?["red_factor"] as JObject;
            if(rf!=null)
            {
                var shortLbl=rf["short_label"]?.ToString()??rf["label"]?.ToString()??"";
                var stars=(int?)rf["stars"]??0;
                tb.Text=$"{shortLbl}{stars}";
                border.Background=new SolidColorBrush(Color.FromArgb(34,217,119,6));
                border.BorderBrush=new SolidColorBrush(Color.Parse("#D97706"));
                ToolTip.SetTip(btn,$"第 5 层（影响祖辈开局适性）：{rf["label"]} {stars}★（点击编辑）");
            }
            else
            {
                tb.Text="+";
                border.Background=new SolidColorBrush(Color.FromArgb(12,120,130,150));
                border.BorderBrush=new SolidColorBrush(Color.FromArgb(45,120,130,150));
                ToolTip.SetTip(btn,"第 5 层曾曾祖辈（点击在右侧编辑）");
            }
        }
    }

    private Grid Branch(string key,string description,int parent)
    {
        var body=new Grid{Name="PedigreeBranchGrid"+key,ColumnDefinitions=new("*,*"),ColumnSpacing=10,RowSpacing=8};
        var heading=new TextBlock{Name="PedigreeBranchTitle"+key,Text="支系 "+key,FontSize=12,
            FontWeight=FontWeight.SemiBold,HorizontalAlignment=HorizontalAlignment.Left,Margin=new Thickness(2,0,0,0)};
        heading.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        ToolTip.SetTip(heading,description);Grid.SetColumnSpan(heading,2);body.Children.Add(heading);
        for(var i=0;i<3;i++)body.Children.Add(cards[parent+i]);
        body.Children.Add(ancestorPanels[parent]);
        body.Children.Add(ancestorPanels[parent+1]);
        var tint=new Border{Background=new SolidColorBrush(key=="A"?Color.FromArgb(14,70,120,210):Color.FromArgb(14,40,150,125)),
            CornerRadius=new CornerRadius(14),IsHitTestVisible=false};
        var container=new Grid{Name="PedigreeBranch"+key};
        var surface=new Border{Padding=new Thickness(10,8,10,10),CornerRadius=new CornerRadius(14),BorderThickness=new Thickness(1),Child=body};
        surface.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        surface.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));
        container.Children.Add(surface);container.Children.Add(tint);
        Grid.SetRow(container,2);Grid.SetColumn(container,key=="A"?0:1);Children.Add(container);
        return body;
    }

    protected override Size MeasureOverride(Size availableSize)
    {
        var width=double.IsFinite(availableSize.Width)?Math.Min(MaxWidth,availableSize.Width):MaxWidth;
        SetCompact(width<CompactBreakpoint);
        var desired=base.MeasureOverride(new Size(width,availableSize.Height));
        return new Size(width,desired.Height);
    }

    private void SetCompact(bool value)
    {
        if(arranged&&compact==value)return;
        arranged=true;compact=value;
        for(var branch=0;branch<2;branch++){
            var parent=branch==0?1:4;var grid=branches[branch];
            grid.RowDefinitions=new(compact?"Auto,Auto,Auto,Auto,Auto,Auto":"Auto,Auto,Auto,Auto");
            void Place(Control ctrl,int row,int column,int span){Grid.SetRow(ctrl,row);Grid.SetColumn(ctrl,column);Grid.SetColumnSpan(ctrl,span);}
            Place(cards[parent],1,0,2);
            if(compact)
            {
                Place(cards[parent+1],2,0,2);
                Place(ancestorPanels[parent],3,0,2);
                Place(cards[parent+2],4,0,2);
                Place(ancestorPanels[parent+1],5,0,2);
            }
            else
            {
                Place(cards[parent+1],2,0,1);
                Place(ancestorPanels[parent],3,0,1);
                Place(cards[parent+2],2,1,1);
                Place(ancestorPanels[parent+1],3,1,1);
            }
            cards[parent].MaxWidth=compact?double.PositiveInfinity:290;
            cards[parent].Width=compact?double.NaN:290;
            cards[parent].HorizontalAlignment=compact?HorizontalAlignment.Stretch:HorizontalAlignment.Center;
            for(var i=0;i<3;i++)cards[parent+i].Margin=new Thickness(compact?10:0,0,0,0);
            for(var i=0;i<=1;i++)ancestorPanels[parent+i].Margin=new Thickness(compact?10:0,2,0,0);
        }
        links?.InvalidateVisual();
    }

    private sealed class Links(PedigreeGraph owner):Control
    {
        public override void Render(DrawingContext context)
        {
            base.Render(context);
            var brush=owner.TryFindResource("SukiControlBorderBrush",out var value)&&value is IBrush b?b:Brushes.SlateGray;
            var pen=new Pen(brush,1.5);
            foreach(var edge in new[]{(0,1),(0,4),(1,2),(1,3),(4,5),(4,6)}){
                var from=owner.cards[edge.Item1];var to=owner.cards[edge.Item2];
                if(owner.compact&&edge.Item1!=0){
                    var a=from.TranslatePoint(new Point(0,from.Bounds.Height/2),this);
                    var z=to.TranslatePoint(new Point(0,to.Bounds.Height/2),this);
                    if(a==null||z==null)continue;
                    var rail=Math.Max(0,Math.Min(a.Value.X,z.Value.X)-7);
                    context.DrawLine(pen,a.Value,new Point(rail,a.Value.Y));
                    context.DrawLine(pen,new Point(rail,a.Value.Y),new Point(rail,z.Value.Y));
                    context.DrawLine(pen,new Point(rail,z.Value.Y),z.Value);
                }else{
                    var a=from.TranslatePoint(new Point(from.Bounds.Width/2,from.Bounds.Height),this);
                    var z=to.TranslatePoint(new Point(to.Bounds.Width/2,0),this);
                    if(a==null||z==null)continue;
                    var mid=edge.Item1==0?a.Value.Y+9:(a.Value.Y+z.Value.Y)/2;
                    context.DrawLine(pen,a.Value,new Point(a.Value.X,mid));
                    context.DrawLine(pen,new Point(a.Value.X,mid),new Point(z.Value.X,mid));
                    context.DrawLine(pen,new Point(z.Value.X,mid),z.Value);
                }
            }
        }
    }
}
