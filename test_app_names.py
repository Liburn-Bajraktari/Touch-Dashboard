import json
import subprocess

def test():
    dump_out = subprocess.check_output(["pw-dump"]).decode()
    data = json.loads(dump_out)
    valid_apps = set()
    for obj in data:
        if obj.get("type") == "PipeWire:Interface:Node":
            props = obj.get("info", {}).get("props", {})
            app_name = props.get("application.name")
            if app_name:
                valid_apps.add(props.get("node.name", ""))
                
    print("Valid App Nodes:")
    for app in valid_apps:
        print(" -", app)
        
test()
