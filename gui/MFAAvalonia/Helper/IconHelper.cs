using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Concurrent;
using System.IO;
using System.Runtime.InteropServices;

namespace MFAAvalonia.Helper;

public static class IconHelper
{
    private const int TitleLogoDipWidth = 28;
    private static readonly ConcurrentDictionary<int, Bitmap> CrispIconsByPixelWidth = new();
    private static readonly Lazy<Bitmap> SourceIcon = new(LoadIconWithFallback);
    private static readonly Lazy<Bitmap> LazyIcon = new(() => GetCrispIcon(GetSystemScale(), TitleLogoDipWidth));
    private static readonly Lazy<WindowIcon> NativeIcon = new(() =>
    {
        using var stream = new MemoryStream(UmaIconRendering.WindowIconBytes(SourceIcon.Value));
        return new WindowIcon(stream);
    });

    public static Bitmap Icon => LazyIcon.Value;
    public static WindowIcon WindowIcon => NativeIcon.Value;

    [DllImport("user32.dll")]
    private static extern uint GetDpiForSystem();

    public static double GetSystemScale()
    {
        if (OperatingSystem.IsWindows())
        {
            try
            {
                var dpi = GetDpiForSystem();
                if (dpi >= 72 && dpi <= 480)
                    return dpi / 96.0;
            }
            catch
            {
                // Fall back to 1.0 in restricted or headless contexts.
            }
        }
        return 1.0;
    }

    public static Bitmap GetCrispIcon(double renderScaling, int dipWidth = TitleLogoDipWidth)
    {
        var scale = double.IsFinite(renderScaling) && renderScaling > 0.5 ? renderScaling : 1.0;
        var targetPx = Math.Clamp((int)Math.Round(dipWidth * scale), 24, 128);
        return CrispIconsByPixelWidth.GetOrAdd(
            targetPx,
            px => UmaIconRendering.ScaleCrisp(SourceIcon.Value, px, 0.58));
    }

    private static Bitmap LoadIconWithFallback()
    {
        try
        {
            var dataDirectory = AppPaths.DataRoot;
            var installDirectory = AppPaths.InstallRoot;

            // 1. 尝试从 interface.json 同目录读取 icon 字段
            var interfacePath = AppPaths.InterfaceJsonPath;
            if (File.Exists(interfacePath))
            {
                var interfaceIcon = TryGetInterfaceIcon(interfacePath);
                if (!string.IsNullOrWhiteSpace(interfaceIcon))
                {
                    var interfaceIconPath = Path.Combine(dataDirectory, interfaceIcon);
                    if (File.Exists(interfaceIconPath))
                    {
                        using var fileStream = File.OpenRead(interfaceIconPath);
                        return UmaIconRendering.LoadBestBitmap(
                            fileStream,
                            interfaceIconPath.EndsWith(".ico", StringComparison.OrdinalIgnoreCase));
                    }
                }
            }

            // 2. 尝试高清奇宝原图 resource/umaa-hishi-miracle.png
            foreach (var root in new[] { dataDirectory, installDirectory })
            {
                if (string.IsNullOrWhiteSpace(root)) continue;
                var hishiPath = Path.Combine(root, "resource", "umaa-hishi-miracle.png");
                if (File.Exists(hishiPath))
                {
                    using var fileStream = File.OpenRead(hishiPath);
                    return UmaIconRendering.LoadBestBitmap(fileStream);
                }
            }

            // 3. 尝试从执行目录加载 logo.ico（提取最大 256px PNG 帧而非 16px 首帧）
            var iconPath = Path.Combine(installDirectory, "Assets", "logo.ico");
            if (!File.Exists(iconPath))
                iconPath = Path.Combine(installDirectory, "assets", "logo.ico");
            if (File.Exists(iconPath))
            {
                using var fileStream = File.OpenRead(iconPath);
                return UmaIconRendering.LoadBestBitmap(fileStream, isIco: true);
            }

            // 4. 尝试从嵌入资源加载
            var uri = new Uri("avares://MFAAvalonia.Core/Assets/logo.ico");
            if (AssetLoader.Exists(uri))
            {
                using var assets = AssetLoader.Open(uri);
                return UmaIconRendering.LoadBestBitmap(assets, isIco: true);
            }

            LoggerHelper.Warning("未找到内嵌图标资源");
            return CreateEmptyImage();
        }
        catch (Exception ex)
        {
            LoggerHelper.Error($"图标加载失败: {ex}");
            return CreateEmptyImage();
        }
    }

    private static string? TryGetInterfaceIcon(string interfacePath)
    {
        try
        {
            var json = JsonHelper.LoadJson<JObject>(interfacePath, null);
            return json?["icon"]?.ToString();
        }
        catch
        {
            return null;
        }
    }

    private static Bitmap CreateEmptyImage()
    {
        // 创建 1x1 透明位图
        var size = new PixelSize(1, 1);
        var dpi = new Vector(96, 96);
        var format = PixelFormat.Rgba8888;

        var writeableBitmap = new WriteableBitmap(size, dpi, format);

        using var buffer = writeableBitmap.Lock();

        var pixels = new byte[buffer.Size.Width * buffer.Size.Height * 4];

        for (var i = 0; i < pixels.Length; i += 4)
        {
            pixels[i] = 0; // R
            pixels[i + 1] = 0; // G
            pixels[i + 2] = 0; // B
            pixels[i + 3] = 0; // A
        }

        Marshal.Copy(pixels, 0, buffer.Address, pixels.Length);

        return writeableBitmap;
    }
}
