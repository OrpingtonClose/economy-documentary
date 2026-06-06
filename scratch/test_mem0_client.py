import os
from mem0 import MemoryClient

api_key = "m0-9cvPiDWE2QJAPGb4Xw82qn2CiWke8i4PiPBkADTe"
print("Initializing MemoryClient...")
try:
    client = MemoryClient(api_key=api_key)
    print("MemoryClient initialized.")
    
    user_id = "test_scenario_agent"
    print("Adding memory to client...")
    res = client.add("The documentary has 3 scenes and a total duration of 105 seconds.", user_id=user_id)
    print("Add response:", res)
    
    print("Retrieving memories...")
    memories = client.get_all(filters={"user_id": user_id})
    print("Memories:", memories)
    
    print("Searching memories...")
    search_res = client.search("documentary scenes", filters={"user_id": user_id})
    print("Search response:", search_res)
except Exception as e:
    import traceback
    traceback.print_exc()
