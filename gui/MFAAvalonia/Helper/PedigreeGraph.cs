using System;
using System.Collections.Generic;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;

namespace MFAAvalonia.Helper;

/// <summary>Seven stable native nodes, grouped branches and six local links.</summary>
public sealed class PedigreeGraph : Grid
{
    private const double CompactBreakpoint=640;
    private readonly Control[] cards;
    private readonly Grid[] branches;
    private readonly Links links;
    private bool compact;
    private bool arranged;
    public int NodeCount=>cards.Length;
    public int LinkCount=>6;

    public PedigreeGraph(Control target,IReadOnlyList<Control> lineage)
    {
        if(lineage.Count!=6)throw new ArgumentException("Six lineage nodes required.");
        Name="ParentPedigreeGraph";MaxWidth=824;HorizontalAlignment=HorizontalAlignment.Center;VerticalAlignment=VerticalAlignment.Top;
        ColumnDefinitions=new("*,*");RowDefinitions=new("Auto,Auto");ColumnSpacing=18;RowSpacing=18;
        cards=[target,lineage[0],lineage[1],lineage[2],lineage[3],lineage[4],lineage[5]];
        foreach(var card in cards){card.Width=double.NaN;card.Margin=new Thickness(0);card.HorizontalAlignment=HorizontalAlignment.Stretch;}
        target.Width=244;target.MaxWidth=244;target.HorizontalAlignment=HorizontalAlignment.Center;
        Grid.SetColumnSpan(target,2);Children.Add(target);
        branches=[Branch("A","亲代 A → 祖辈 AA / AB",1),Branch("B","亲代 B → 祖辈 BA / BB",4)];
        links=new Links(this){Name="PedigreeLocalLinks",IsHitTestVisible=false};
        Grid.SetColumnSpan(links,2);Grid.SetRowSpan(links,2);Children.Add(links);
        LayoutUpdated+=(_,_)=>links.InvalidateVisual();SetCompact(false);
    }

    private Grid Branch(string key,string description,int parent)
    {
        var body=new Grid{Name="PedigreeBranchGrid"+key,ColumnDefinitions=new("*,*"),ColumnSpacing=10,RowSpacing=12};
        var heading=new TextBlock{Name="PedigreeBranchTitle"+key,Text="支系 "+key,FontSize=12,
            FontWeight=FontWeight.SemiBold,HorizontalAlignment=HorizontalAlignment.Left,Margin=new Thickness(2,0,0,0)};
        heading.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        ToolTip.SetTip(heading,description);Grid.SetColumnSpan(heading,2);body.Children.Add(heading);
        for(var i=0;i<3;i++)body.Children.Add(cards[parent+i]);
        var tint=new Border{Background=new SolidColorBrush(key=="A"?Color.FromArgb(14,70,120,210):Color.FromArgb(14,40,150,125)),
            CornerRadius=new CornerRadius(14),IsHitTestVisible=false};
        var container=new Grid{Name="PedigreeBranch"+key};
        var surface=new Border{Padding=new Thickness(12,8,12,10),CornerRadius=new CornerRadius(14),BorderThickness=new Thickness(1),Child=body};
        surface.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        surface.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));
        container.Children.Add(surface);container.Children.Add(tint);
        // Explicit A/B labels supplement the faint branch tint; colors never
        // encode ownership or availability. Hit testing stays with each card.
        Grid.SetRow(container,1);Grid.SetColumn(container,key=="A"?0:1);Children.Add(container);
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
            grid.RowDefinitions=new(compact?"Auto,Auto,Auto,Auto":"Auto,Auto,Auto");
            void Place(int index,int row,int column,int span){var card=cards[index];Grid.SetRow(card,row);Grid.SetColumn(card,column);Grid.SetColumnSpan(card,span);}
            Place(parent,1,0,2);Place(parent+1,2,0,compact?2:1);Place(parent+2,compact?3:2,compact?0:1,compact?2:1);
            cards[parent].MaxWidth=compact?double.PositiveInfinity:232;
            cards[parent].Width=compact?double.NaN:232;
            cards[parent].HorizontalAlignment=compact?HorizontalAlignment.Stretch:HorizontalAlignment.Center;
            // Each compact branch reserves its own small connector gutter.
            for(var i=0;i<3;i++)cards[parent+i].Margin=new Thickness(compact?10:0,0,0,0);
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
