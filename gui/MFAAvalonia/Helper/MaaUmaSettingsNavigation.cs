using System.Linq;
using Avalonia.Controls;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Views.Pages;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

public static class MaaUmaSettingsNavigation
{
    public static void Open(Control source,string section="connection")
    {
        var top=TopLevel.GetTopLevel(source)??Instances.TopLevel;
        if(top==null)return;
        var menu=top.GetVisualDescendants().OfType<SukiSideMenu>().FirstOrDefault();
        var item=menu?.FooterMenuItems?.OfType<SukiSideMenuItem>().FirstOrDefault(row=>row.PageContent is SettingsView);
        if(menu==null||item==null)return;
        menu.SelectedItem=item;
        Dispatcher.UIThread.Post(()=> {
            var name=section switch {"notifications"=>"MaaUmaNotificationSettings","updates"=>"MaaUmaUpdateGroup",_=>"MaaUmaConnectionSettings"};
            var target=top.GetVisualDescendants().OfType<Control>().FirstOrDefault(control=>control.Name==name);
            target?.BringIntoView();target?.Focus();
        },DispatcherPriority.Loaded);
    }
}
