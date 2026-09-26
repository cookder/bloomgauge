import AppKit
let size=1024.0
let image=NSImage(size:NSSize(width:size,height:size))
image.lockFocus()
NSColor(calibratedRed:0.055,green:0.16,blue:0.115,alpha:1).setFill()
NSBezierPath(roundedRect:NSRect(x:24,y:24,width:976,height:976),xRadius:215,yRadius:215).fill()
for index in 0..<8{
    NSGraphicsContext.saveGraphicsState()
    let transform=NSAffineTransform();transform.translateX(by:512,yBy:512);transform.rotate(byDegrees:Double(index)*45);transform.concat()
    NSColor(calibratedRed:0.78,green:0.93,blue:0.45,alpha:1).setFill()
    NSBezierPath(roundedRect:NSRect(x:-52,y:98,width:104,height:245),xRadius:52,yRadius:52).fill()
    NSGraphicsContext.restoreGraphicsState()
}
NSColor(calibratedRed:0.78,green:0.93,blue:0.45,alpha:1).setFill()
NSBezierPath(ovalIn:NSRect(x:464,y:464,width:96,height:96)).fill()
image.unlockFocus()
let bitmap=NSBitmapImageRep(data:image.tiffRepresentation!)!
try bitmap.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
