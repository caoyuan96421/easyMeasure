Camera SDKs are installed separately; they are not distributed in this repository.

ToupCam microscope:
  Obtain the ToupTek camera SDK from the camera vendor. Place its toupcam.py
  binding and matching 64-bit native library together in this folder. On Windows
  the library is toupcam.dll. Keep the vendor's own license and notices with it.
  Simulation works without either file.

ZWO spectrometer:
  Install the ZWO ASI SDK. Supply ASICamera2.dll through the camera instrument's
  sdk_path option, install it with ASIStudio, or place it at
  <repository>/vendor/zwo/ASICamera2.dll when running from a checkout.
  Simulation does not load the DLL.
