import Cocoa

// Draws the 1024 px app icon: counter drums on a meter's enamel plate, the last drum red
// like the tenths on an electricity meter. Usage: make-icon <output.png>

func color(_ hex: UInt32, alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
}

let size = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

let body = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185)
color(0xE9ECE7).setFill()
body.fill()
color(0xC9CEC7).setStroke()
body.lineWidth = 6
body.stroke()

let plate = NSRect(x: 168, y: 382, width: 688, height: 260)
color(0xD3D8D1).setFill()
NSBezierPath(roundedRect: plate, xRadius: 34, yRadius: 34).fill()

let font = NSFont(name: "DIN Condensed Bold", size: 200) ?? NSFont.boldSystemFont(ofSize: 170)
let drumWidth: CGFloat = 142, drumHeight: CGFloat = 212, gap: CGFloat = 12, redGap: CGFloat = 18
let digits = ["1", "2", "8", "4"]
let rowWidth = drumWidth * 4 + gap * 3 + redGap
var x = plate.midX - rowWidth / 2
for (i, digit) in digits.enumerated() {
    let drum = NSRect(x: x, y: plate.midY - drumHeight / 2, width: drumWidth, height: drumHeight)
    let shape = NSBezierPath(roundedRect: drum, xRadius: 16, yRadius: 16)
    (i == digits.count - 1 ? color(0xC3312B) : color(0x16191B)).setFill()
    shape.fill()

    let text = NSAttributedString(string: digit, attributes: [.font: font, .foregroundColor: color(0xF2F2EE)])
    let baseline = drum.midY - font.capHeight / 2
    text.draw(at: NSPoint(x: drum.midX - text.size().width / 2, y: baseline + font.descender))

    // the curve of the drum: darker towards its top and bottom edges
    NSGraphicsContext.saveGraphicsState()
    shape.addClip()
    NSGradient(colorsAndLocations: (color(0, alpha: 0.55), 0), (color(0, alpha: 0), 0.3),
               (color(0, alpha: 0), 0.7), (color(0, alpha: 0.55), 1))!.draw(in: drum, angle: 90)
    NSGraphicsContext.restoreGraphicsState()

    x += drumWidth + (i == digits.count - 2 ? redGap + gap : gap)
}

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
