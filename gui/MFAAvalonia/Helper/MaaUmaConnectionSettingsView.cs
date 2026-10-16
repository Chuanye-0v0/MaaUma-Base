using Avalonia;
using Avalonia.Controls;
using Avalonia.Data;
using Avalonia.Data.Converters;
using Avalonia.Layout;
using System.Linq;
using MFAAvalonia.Helper.Converters;
using MFAAvalonia.Views.UserControls.Settings;
using Microsoft.Extensions.DependencyInjection;

namespace MFAAvalonia.Helper;

public sealed class MaaUmaConnectionSettingsView : UserControl
{
    public MaaUmaConnectionSettingsView() : this(null,
        App.Services.GetRequiredService<ConnectSettingsUserControl>(),Instances.RootViewModel)
    {
        this.Bind(DataContextProperty,new Binding("ActiveTab.TaskQueueViewModel") {
            Source=Instances.InstanceTabBarViewModel });
    }

    // Offline checks supply the same selection model shape without a device.
    public MaaUmaConnectionSettingsView(object? selectionModel,Control advanced,object runtimeState)
    {
        Name="MaaUmaConnectionSettings";DataContext=selectionModel;
        var body=new StackPanel {Spacing=14};
        body.Children.Add(new TextBlock {Text="所有任务共用当前实例的模拟器连接。",TextWrapping=Avalonia.Media.TextWrapping.Wrap});
        var controller=new ComboBox {Name="MaaUmaSystemController",HorizontalAlignment=HorizontalAlignment.Stretch,
            DisplayMemberBinding=new Binding("DisplayName")};
        controller.Bind(ItemsControl.ItemsSourceProperty,new Binding("ControllerOptions"));
        controller.Bind(ComboBox.SelectedItemProperty,new Binding("SelectedController"){Mode=BindingMode.TwoWay});
        controller.Bind(IsEnabledProperty,new MultiBinding {
            Bindings={new Binding("CurrentInstanceIdle"){Source=runtimeState},new Binding("LockController"){Source=runtimeState}},
            Converter=new FuncMultiValueConverter<object,bool>(values=>{
                var flags=values.ToArray();return flags.Length==2&&flags[0] is true&&flags[1] is not true;})});
        var device=new ComboBox {Name="MaaUmaSystemDevice",HorizontalAlignment=HorizontalAlignment.Stretch,
            DisplayMemberBinding=new Binding {Converter=new DeviceDisplayConverter()}};
        device.Bind(ItemsControl.ItemsSourceProperty,new Binding("Devices"));
        device.Bind(ComboBox.SelectedItemProperty,new Binding("CurrentDevice"){Mode=BindingMode.TwoWay});
        device.Bind(ComboBox.PlaceholderTextProperty,new Binding("CurrentDevicePlaceholderText"));
        device.Bind(IsEnabledProperty,new Binding("CurrentInstanceIdle"){Source=runtimeState});
        body.Children.Add(new TextBlock {Text="控制器"});body.Children.Add(controller);
        body.Children.Add(new TextBlock {Text="当前模拟器"});body.Children.Add(device);
        var actions=new WrapPanel {Orientation=Orientation.Horizontal};
        foreach(var row in new[]{("刷新列表","RefreshCommand"),("手动配置","CustomAdbCommand"),("重新连接","ReconnectCommand")}) {
            var button=new Button {Content=row.Item1,Margin=new Thickness(0,0,8,8)};
            button.Bind(Button.CommandProperty,new Binding(row.Item2));
            button.Bind(IsEnabledProperty,new Binding("CurrentInstanceIdle"){Source=runtimeState});
            actions.Children.Add(button);
        }
        body.Children.Add(actions);
        body.Children.Add(new Expander {Header="高级连接选项",Content=advanced,HorizontalAlignment=HorizontalAlignment.Stretch});
        Content=body;
    }
}
