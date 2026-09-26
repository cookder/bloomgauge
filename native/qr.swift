import AppKit
import CoreImage

guard CommandLine.arguments.count == 2,
      let filter = CIFilter(name: "CIQRCodeGenerator") else { exit(1) }
filter.setValue(Data(CommandLine.arguments[1].utf8), forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let output = filter.outputImage else { exit(1) }
let image = output.transformed(by: CGAffineTransform(scaleX: 6, y: 6))
let bounds = image.extent.insetBy(dx: -24, dy: -24)
let white = CIImage(color: CIColor.white).cropped(to: bounds)
guard let cg = CIContext().createCGImage(image.composited(over: white), from: bounds) else { exit(1) }
let bitmap = NSBitmapImageRep(cgImage: cg)
guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
FileHandle.standardOutput.write(png)
