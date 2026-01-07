The TIKAL Library provides two different APIs of communicating with the toys

The 'High Level' API isn't implemented yet

The 'Low Level' API provides LovenseConnectionBuilder (Implementation of the abstract the
ToyConnectionBuilder) to scan and connect to toys. LovenseConnectionBuilder produces and hands over
LovenseBLED (Implementation of the abstract ToyBLED) to control the toy. Both classes are mostly async.
You can use ToyCache to remember toy model names in-between sessions.
