import json
from django.test import TestCase
from amc.models import CharacterLocation
from amc.locations import process_player

class LocationsTests(TestCase):
  async def test_monitor_location(self):
    data = json.loads("""{"data":[{"OwnEventGuids":{}, "JoinedEventGuids":["5B11926A45D1869C3AA6309F3F564829"], "CharacterGuid":"E603C74946EFF3F8834C9AAB3D0E3181", "BestLapTime":0.0, "bIsAdmin":true, "OwnCompanyGuid":"140F7EE64C640E282A1768A14B550613", "Levels":{}, "VehicleKey":"Fortem", "bIsHost":false, "PlayerName":"freeman", "UniqueID":"76561198378447512", "JoinedCompanyGuid":"0000", "CustomDestinationAbsoluteLocation":{"Z":0.0, "X":0.0, "Y":0.0}, "GridIndex":0, "Location":{"Z":-13839.230637516, "X":-189475.87476375, "Y":-1288.5995332908}}]}""")
    location = data['data'][0]
    await process_player(location)
    self.assertTrue(
      await CharacterLocation.objects.aexists()
    )
