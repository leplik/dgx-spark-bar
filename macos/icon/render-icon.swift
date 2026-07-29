// Draws the app icon: the indicator lamp the Spark does not have, sunk into a
// dark panel. One glyph, readable at 16pt, and the same green as the menu bar
// dot in DGXSparkBarApp.swift.
//
//   swift render-icon.swift <pixels> <output.png>

import AppKit

let px = CommandLine.arguments.count > 1 ? Double(CommandLine.arguments[1]) ?? 1024 : 1024
let out = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "icon.png"

let S = CGFloat(px)
func u(_ v: CGFloat) -> CGFloat { v * S / 1024 }   // everything is authored at 1024

guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: Int(px), pixelsHigh: Int(px),
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
) else { exit(1) }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext
ctx.setShouldAntialias(true)

// macOS icons do not fill their canvas: the art sits in a rounded square with
// air around it, or it looks oversized next to every other icon in the Dock.
let inset = u(100)
let body = NSRect(x: inset, y: inset, width: S - inset * 2, height: S - inset * 2)
let squircle = NSBezierPath(roundedRect: body, xRadius: u(185), yRadius: u(185))

squircle.addClip()
NSGradient(starting: NSColor(calibratedRed: 0.20, green: 0.22, blue: 0.25, alpha: 1),
           ending:   NSColor(calibratedRed: 0.07, green: 0.08, blue: 0.09, alpha: 1))?
    .draw(in: body, angle: -90)

// A brushed sheen across the upper third, so the panel reads as metal.
NSGradient(starting: NSColor(calibratedWhite: 1, alpha: 0.10),
           ending:   NSColor(calibratedWhite: 1, alpha: 0.0))?
    .draw(in: NSRect(x: body.minX, y: body.midY, width: body.width, height: body.height / 2),
          angle: -90)

let centre = NSPoint(x: body.midX, y: body.midY)

// Glow first, under everything: a lamp lights the panel around it.
NSGradient(colors: [
    NSColor(calibratedRed: 0.20, green: 0.85, blue: 0.40, alpha: 0.55),
    NSColor(calibratedRed: 0.20, green: 0.85, blue: 0.40, alpha: 0.0),
// Only drawsBefore: the far end is transparent, but filling past it would still
// paint the whole squircle and cost nothing but time.
])?.draw(fromCenter: centre, radius: u(120), toCenter: centre, radius: u(340),
         options: .drawsBeforeStartingLocation)

// The bezel it is sunk into.
NSColor(calibratedWhite: 0.04, alpha: 1).setFill()
NSBezierPath(ovalIn: NSRect(x: centre.x - u(215), y: centre.y - u(215),
                            width: u(430), height: u(430))).fill()
NSColor(calibratedWhite: 1, alpha: 0.09).setStroke()
let rim = NSBezierPath(ovalIn: NSRect(x: centre.x - u(212), y: centre.y - u(212),
                                      width: u(424), height: u(424)))
rim.lineWidth = u(6)
rim.stroke()

// The lamp. Lit from slightly above, like the real thing.
let lens = NSPoint(x: centre.x, y: centre.y + u(40))

// Clipped to its own circle: a radial gradient asked to fill past its end
// radius fills the entire clip region, and unclipped that is the whole panel.
NSGraphicsContext.current!.saveGraphicsState()
NSBezierPath(ovalIn: NSRect(x: centre.x - u(170), y: centre.y - u(170),
                            width: u(340), height: u(340))).setClip()
NSGradient(colors: [
    NSColor(calibratedRed: 0.62, green: 1.00, blue: 0.68, alpha: 1),
    NSColor(calibratedRed: 0.16, green: 0.80, blue: 0.34, alpha: 1),
    NSColor(calibratedRed: 0.05, green: 0.50, blue: 0.20, alpha: 1),
])?.draw(fromCenter: lens, radius: u(12), toCenter: centre, radius: u(200), options: [.drawsBeforeStartingLocation, .drawsAfterEndingLocation])

// Specular highlight: the giveaway that a circle is a glass lens and not a dot.
NSGradient(colors: [NSColor(calibratedWhite: 1, alpha: 0.75),
                    NSColor(calibratedWhite: 1, alpha: 0.0)])?
    .draw(fromCenter: NSPoint(x: centre.x - u(50), y: centre.y + u(75)), radius: u(4),
          toCenter: NSPoint(x: centre.x - u(50), y: centre.y + u(75)), radius: u(95),
          options: .drawsBeforeStartingLocation)
NSGraphicsContext.current!.restoreGraphicsState()

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try! png.write(to: URL(fileURLWithPath: out))
