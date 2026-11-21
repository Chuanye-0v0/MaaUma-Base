using System;
using System.Collections.Generic;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed class UmaCareerPauseDates : UserControl
{
    private sealed record Row(string? Id,ComboBox Year,ComboBox Month,ComboBox Half,CheckBox Enabled,Control View);
    private readonly StackPanel rows=new(){Spacing=8};
    private readonly List<Row> values=new();
    private bool loading;
    public event Action? Changed;
    public UmaCareerPauseDates()
    {
        var add=new Button {Name="PacerAddDateStop",Content="添加日期停点"};
        add.Click+=(_,_)=>{Add(null,2,7,"first",true);Changed?.Invoke();};
        Content=new StackPanel {Spacing=8,Children={
            new TextBlock {Text="按育成日期暂停（上半月 / 下半月）",TextWrapping=Avalonia.Media.TextWrapping.Wrap},rows,add}};
    }
    private void Add(string? id,int year,int month,string half,bool enabled)
    {
        if(year is <1 or >3||month is <1 or >12||half is not ("first" or "second"))
            throw new FormatException("停点日期读取未完成，请重新读取计划。");
        var y=new ComboBox {ItemsSource=new[]{"初级","经典","高级"},SelectedIndex=year-1};
        var m=new ComboBox {ItemsSource=new[]{"1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"},SelectedIndex=month-1};
        var h=new ComboBox {ItemsSource=new[]{"上半月","下半月"},SelectedIndex=half=="second"?1:0};
        var on=new CheckBox {Content="启用",IsChecked=enabled,VerticalAlignment=VerticalAlignment.Center};
        var remove=new Button {Content="删除"};
        var grid=new Grid {ColumnDefinitions=new("*,*,*,Auto,Auto"),ColumnSpacing=5};
        var controls=new Control[]{y,m,h,on,remove};for(var i=0;i<controls.Length;i++){Grid.SetColumn(controls[i],i);grid.Children.Add(controls[i]);}
        var row=new Row(id,y,m,h,on,grid);values.Add(row);rows.Children.Add(grid);
        foreach(var choice in new[]{y,m,h})choice.SelectionChanged+=(_,_)=>{if(!loading)Changed?.Invoke();};
        on.IsCheckedChanged+=(_,_)=>{if(!loading)Changed?.Invoke();};
        remove.Click+=(_,_)=>{values.Remove(row);rows.Children.Remove(grid);Changed?.Invoke();};
    }
    public void Load(JArray? dates)
    {
        loading=true;
        try {values.Clear();rows.Children.Clear();foreach(var date in dates??new JArray())
            Add((string?)date["id"],(int?)date["year_index"]??0,(int?)date["month"]??0,
                (string?)date["half"]??"",(bool?)date["enabled"]!=false);}
        finally {loading=false;}
    }
    public JArray Read()
    {
        var result=new JArray();foreach(var row in values){
            if(row.Year.SelectedIndex is <0 or >2||row.Month.SelectedIndex is <0 or >11||row.Half.SelectedIndex is <0 or >1)
                throw new FormatException("请填完整停点年级、月份和上半月/下半月。");
            var date=new JObject {["year_index"]=row.Year.SelectedIndex+1,["month"]=row.Month.SelectedIndex+1,
                ["half"]=row.Half.SelectedIndex==1?"second":"first",["enabled"]=row.Enabled.IsChecked==true};
            if(!string.IsNullOrWhiteSpace(row.Id))date["id"]=row.Id;result.Add(date);
        }return result;
    }
}
