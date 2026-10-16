using System;
using System.IO;
using System.Runtime.InteropServices;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media.Imaging;
using Avalonia.Platform;

namespace MFAAvalonia.Helper;

/// <summary>Original geometric MaaUma mark; no external portrait or font input.</summary>
public static class IconHelper
{
    private static readonly Lazy<Bitmap> SourceIcon = new(CreateMark);
    private static readonly Lazy<WindowIcon> NativeIcon = new(() =>
    {
        using var stream = new MemoryStream(UmaIconRendering.WindowIconBytes(SourceIcon.Value));
        return new WindowIcon(stream);
    });
    public static Bitmap Icon => SourceIcon.Value;
    public static WindowIcon WindowIcon => NativeIcon.Value;

    private static Bitmap CreateMark()
    {
        const int size = 128;
        var image = new WriteableBitmap(new PixelSize(size, size), new Vector(96, 96),
            PixelFormat.Rgba8888, AlphaFormat.Unpremul);
        (double X, double Y)[] letter = { (26,96), (26,32), (40,32), (64,62),
            (88,32), (102,32), (102,96), (86,96), (86,58), (64,84), (42,58), (42,96) };
        var row = new byte[size * 4];
        using (var buffer = image.Lock())
        {
            for (var y = 0; y < size; y++)
            {
                Array.Clear(row);
                for (var x = 0; x < size; x++)
                {
                    var px = x + .5; var py = y + .5;
                    var dx = Math.Max(28 - px, Math.Max(0, px - 100));
                    var dy = Math.Max(28 - py, Math.Max(0, py - 100));
                    if (dx * dx + dy * dy > 24 * 24) continue;
                    var inside = false;
                    for (int i = 0, j = letter.Length - 1; i < letter.Length; j = i++)
                        if ((letter[i].Y > py) != (letter[j].Y > py) &&
                            px < (letter[j].X - letter[i].X) * (py - letter[i].Y) /
                            (letter[j].Y - letter[i].Y) + letter[i].X) inside = !inside;
                    var offset = x * 4;
                    row[offset] = inside ? (byte)255 : (byte)37;
                    row[offset + 1] = inside ? (byte)255 : (byte)99;
                    row[offset + 2] = inside ? (byte)255 : (byte)235;
                    row[offset + 3] = 255;
                }
                Marshal.Copy(row, 0, buffer.Address + y * buffer.RowBytes, row.Length);
            }
        }
        // Skia resizing accepts an immutable decoded bitmap, rather than the
        // writable backing type used while generating these original pixels.
        using var encoded = new MemoryStream();
        image.Save(encoded);
        image.Dispose();
        encoded.Position = 0;
        return new Bitmap(encoded);
    }
}
