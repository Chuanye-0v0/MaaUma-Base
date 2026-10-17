using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using System;
using System.Threading.Tasks;

namespace MFAAvalonia.Helper;

/// <summary>One explicit click submits the latest locally recorded task failure.</summary>
public sealed class MaaUmaTaskFailureReportButton : Button
{
    private bool reporting;

    public MaaUmaTaskFailureReportButton()
    {
        Content="提交失败报告";
        ToolTip.SetTip(this,"仅向维护者提交最近失败的任务、版本和脱敏异常，不含配置或截图。");
        Click+=SubmitFailure;
    }

    private async void SubmitFailure(object? sender,RoutedEventArgs args)
    {
        if(reporting)return;
        reporting=true;IsEnabled=false;
        try {
            var panel=new MaaUmaErrorFeedbackPanel();
            panel.SetLatestTaskFailure();
            var copy=new Button {Content="复制脱敏报告"};
            var body=new StackPanel {Margin=new Thickness(20),Spacing=12,Children={panel,copy}};
            var dialog=new Window {Title="任务失败报告",Width=620,Height=300,Content=body,
                WindowStartupLocation=WindowStartupLocation.CenterOwner};
            copy.Click+=async(_,_)=>{
                if(dialog.Clipboard is { } clipboard)await panel.CopyCurrentAsync(clipboard.SetTextAsync);
            };
            if(TopLevel.GetTopLevel(this) is Window owner)dialog.Show(owner);
            else dialog.Show();
            await panel.SubmitCurrentAsync();
        }catch {
            // Reporting never changes the task queue or propagates into gameplay.
            ToolTip.SetTip(this,"报告未能打开，请重试或使用导出日志。");
        }finally{reporting=false;IsEnabled=true;}
    }
}
