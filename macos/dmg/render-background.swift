// Draws the DMG window background. Run through make-dmg.sh, which renders it at
// 1x and 2x and folds both into one HiDPI background.tiff.
//
// Coordinates here are quoted top-left, because that is how Finder stores icon
// positions in .DS_Store and the two have to agree. AppKit draws bottom-left, so
// everything goes through fy().
//
//   swift render-background.swift <scale> <output.png>

import AppKit

let scale = CommandLine.arguments.count > 1 ? Int(CommandLine.arguments[1]) ?? 1 : 1
let out = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "background.png"

let W: CGFloat = 540, H: CGFloat = 380
func fy(_ y: CGFloat) -> CGFloat { H - y }

// Must match ICON_Y in make-dmg.sh: the arrow is drawn between two icons this
// file never sees.
let iconY: CGFloat = 190
let appX: CGFloat = 140, dropX: CGFloat = 400

guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: Int(W) * scale, pixelsHigh: Int(H) * scale,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
) else { exit(1) }
// Drawing then happens in points and the scale factor is handled for us.
rep.size = NSSize(width: W, height: H)

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// A light background on purpose: Finder draws the icon labels in black over it
// and does not adapt them to a dark image.
NSGradient(starting: NSColor(calibratedRed: 0.984, green: 0.984, blue: 0.992, alpha: 1),
           ending:   NSColor(calibratedRed: 0.925, green: 0.929, blue: 0.945, alpha: 1))?
    .draw(in: NSRect(x: 0, y: 0, width: W, height: H), angle: -90)

// The dot the whole project exists for.
NSColor.systemGreen.setFill()
NSBezierPath(ovalIn: NSRect(x: 176, y: fy(78), width: 13, height: 13)).fill()

func text(_ s: String, _ font: NSFont, _ color: NSColor, centeredAt x: CGFloat, top y: CGFloat) {
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
    let size = (s as NSString).size(withAttributes: attrs)
    (s as NSString).draw(at: NSPoint(x: x - size.width / 2, y: fy(y) - size.height), withAttributes: attrs)
}

text("DGX Spark Bar", .systemFont(ofSize: 21, weight: .semibold),
     NSColor(calibratedWhite: 0.13, alpha: 1), centeredAt: W / 2 + 12, top: 62)
text("A status LED for the NVIDIA DGX Spark",
     .systemFont(ofSize: 12, weight: .regular),
     NSColor(calibratedWhite: 0.45, alpha: 1), centeredAt: W / 2, top: 96)

// Arrow between the two icons. It starts and ends well clear of them so it never
// runs underneath a 128pt icon or its label.
let arrow = NSBezierPath()
let y = fy(iconY)
let x0 = appX + 88, x1 = dropX - 88
arrow.move(to: NSPoint(x: x0, y: y))
arrow.line(to: NSPoint(x: x1 - 13, y: y))
arrow.lineWidth = 3
arrow.lineCapStyle = .round
NSColor(calibratedWhite: 0.62, alpha: 1).setStroke()
arrow.stroke()

let head = NSBezierPath()
head.move(to: NSPoint(x: x1, y: y))
head.line(to: NSPoint(x: x1 - 15, y: y + 9))
head.line(to: NSPoint(x: x1 - 15, y: y - 9))
head.close()
NSColor(calibratedWhite: 0.62, alpha: 1).setFill()
head.fill()

text("Drag it onto Applications", .systemFont(ofSize: 12, weight: .medium),
     NSColor(calibratedWhite: 0.42, alpha: 1), centeredAt: W / 2, top: 300)

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try! png.write(to: URL(fileURLWithPath: out))
