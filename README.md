# TIKAL
'Toys I Know And Love' is a Python Library to connect to and control Lovense Toys (https://www.lovense.com) via Bluetooth.
Only SolacePro and Gush2 (Toys that I own and therefore can test on) are fully supported, other toys might work fully,
likely work at least partially, but also may not work at all.
If you have other toys I would be very thankful if you report to me whether they work or not.
The library currently only supports lovense toys and will likely remain so for the foreseeable future.
Should I get my hands on a different toy that I like then I'll likely add it to the library

## Installation and Usage
The Library is currently in Alpha and I do not yet make it available on PyPI
if you like to work on the library you'll need clone the Repo and install the Bleak Library "pip install bleak".
I use python 3.14 but any (reasonably new) version is likely to work
For Usage, please see (./examples/) for (heavily commented) usage examples.

### Low Level API
The 'Low Level' API provides LovenseConnectionBuilder (Implementation of the abstract the
ToyConnectionBuilder) to scan and connect to toys. LovenseConnectionBuilder produces and hands over
LovenseBLED (Implementation of the abstract ToyBLED) to control the toy. Both classes are mostly async.
You can use ToyCache to remember toy model names in-between sessions.

### High Level API
Not implemented yet

## What you should know:
The app directly connects to the toy (without using the official api).
This is possible thanks to reverse engineering efforts by https://docs.buttplug.io/docs/stpihkal/protocols/lovense/
Doing this comes with several advantages and disadvantages:

### Advantages:
- I was able to learn a bit about implementing bluetooth connections :)
- The app does not rely on LovenseConnect or LovenseRemote. Furthermore, it does not need the Lovense USB Dongle
(normally needed to control the toys from Windows PCs for whatever reason)

### Disadvantages:
- All information about how to communicate with the toys came from reverse-engineering efforts (mostly by stpihkal and a little by me)
The information is vastly incomplete, which is the reason why toy support is lacking.
- I don't know how Lovense sends patterns to the device. For now, I don't provide any functionality to send patterns
(Instead relying on adjusting the toy repeatadly via the Intensity control functions).
To avoid overloading the device, these changes should be limited.
I found that sending two commands (e.g. thrust and depth) every 100ms is ok. Shorter changes may be ok too, I just didn't test that.
- Toy Identification (model_name) needs to be done manually (letting the user select a toy from the LOVENSE_TOY_NAMES dict keys).
Toy Cache can help a bit, storing the names of connected toys for later sessions.

### More info on automatic toy identification problems:
The correct model name is needed to send the correct commands to the toy.
Unfortunately, identifying the model name is difficult.
Some Lovense Bluetooth names consist of LVS-<\model>\ were model is the correct mode name.
Most However consist of LVS-<\Identifier>\<\Firmware Version>\ were Identifier is one letter.
The Mapping of Identifier to model name is not public, and there is no good pattern to map the Identifier to the model name.
Some models like Nora have multiple Identifiers (A or C), while some models share identifiers (Max and SolacePro both identify as B)

## Help me:
You can help this project by:
- Testing toys and reporting if they work fully, partially, or not at all
- Not all device capabilites are implemented (e.g. things like turning on/off lights, getting and controlling patterns of toys that have this capability).
You can help by implementing the missing ones.
https://docs.buttplug.io/docs/stpihkal/protocols/lovense/ Includes a few missing capabilities, but there might be other ones that aren't documented even there.
- Improving code readability or documentation
- Reporting any bugs you encounter

## Notes about reporting:
If you do report, please follow these rules:
- be polite (I'm a human, you know)
- provide me with the toy model that you used e.g. Nora, SolacePro
- Note that the Software is provided as is and that I have absolutely no obligation to do anything.
I'll do my best but I do have other stuff to do and my coding skills have limits

## Affiliation
Please note that I am NOT affiliated to the Lovense Company in any way. All bugs and issues with this software are NOT their problem.

## License
This project is licensed under MIT. See LICENSE.txt
