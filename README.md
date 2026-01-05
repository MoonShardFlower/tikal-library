# Lovense-Python-BL-Control
Python Library to connect to and control Lovense Toys (https://www.lovense.com) via Bluetooth.
Only SolacePro and Gush2 (Toys that I own and therefore can test on) are fully supported, other toys might work fully, likely work at least partially, but also may not work at all.
If you have other toys I would be very thankful if you report to me whether they work or not.

## Installation and Usage
Besides cloning the Repo, you'll need the Bleak Library "pip install bleak". I use python 3.14 but any (reasonably new) version is likely to work
For Usage, please see (./examples/) for (heavily commented) usage examples. You can also take a look at src/LovenseToy.py, which contains the implementations of all functions that you may use.

## What you should know:
The app directly connects to the toy (without using the official api). This is possible thanks to reverse engineering efforts by https://docs.buttplug.io/docs/stpihkal/protocols/lovense/
Doing this comes with several advantages and disadvantages:

### Advantages:
- I was able to learn a bit about implementing bluetooth connections :)
- The app does not rely on LovenseConnect or LovenseRemote. Furthermore, it does not need the Lovense USB Dongle (normally needed to control the toys from Windows PCs for whatever reason)

### Disadvantages:
- All information about how to communicate with the toys came from reverse-engineering efforts by other people and through a bit of trial and error on my part. The information is vastly incomplete, which is the reason why toy support is lacking.
- I don't know how Lovense sends patterns to the device. For now, I don't provide any functionality to send patterns (Instead relying on adjusting the toy repeatadly via the Intensity control functions). To avoid overloading the device, these changes should be limited. I found that sending two commands (e.g. thrust and depth) every 100ms is ok. Shorter changes may be ok too, I just didn't test that.
- Toy Identification (model_name) needs to be done manually (either you let the user select a toy from the LOVENSE_TOY_NAMES dict keys or you figure out a way to somewhat reliably figure out the model automatically). Toy Cache can help a bit, storing the names of connected toys for later sessions.

## Help me:
You can help this project by:
- Testing toys and reporting if they work fully, partially, or not at all
- Not all device capabilites are implemented (e.g. things like turning on/off lights, getting and controlling patterns of toys that have this capability). You can help by implementing the missing ones. https://docs.buttplug.io/docs/stpihkal/protocols/lovense/ Includes a few missing capabilities, but there might be other ones that aren't documented even there.
- Improving code readability or documentation
- Reporting any bugs you encounter

## Notes about reporting:
If you do report, please follow these rules:
- be polite (I'm a human, you know)
- provide me with the toy model that you used e.g. Nora, SolacePro
- Note that the Software is provided as is and that I have absolutely no obligation to do anything. I'll do my best but I do have other stuff to do and my coding skills have limits

## Affiliation
Please note that I am in no way affiliated to the Lovense Company. Any bugs or issues with this software are NOT their problem.

## License
This project is licensed under MIT. See LICENSE.txt
