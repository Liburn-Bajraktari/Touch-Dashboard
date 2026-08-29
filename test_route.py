import subprocess

def test_route():
    pw_links = subprocess.check_output(["pw-link", "-l"]).decode().splitlines()
    
    # build graph
    # node -> list of dest nodes
    graph = {}
    
    current_out_node = None
    
    for line in pw_links:
        if not line.startswith((" ", "\t")):
            stripped = line.strip()
            if ":" in stripped:
                current_out_node = stripped.split(":")[0]
                if current_out_node not in graph:
                    graph[current_out_node] = []
        elif current_out_node and "|->" in line:
            dest = line.split("|->")[1].strip()
            dest_node = dest.split(":")[0]
            graph[current_out_node].append(dest_node)

    # Find all nodes that HAVE a capture output port.
    # We can use pw-link -o
    pw_out = subprocess.check_output(["pw-link", "-o"]).decode().splitlines()
    capture_nodes = set()
    for line in pw_out:
        line = line.strip()
        if ":" in line and "capture" in line:
            node = line.split(":")[0]
            capture_nodes.add(node)
            
    # Now find all ancestors of capture_nodes
    # Reverse the graph
    rev_graph = {}
    for src, dests in graph.items():
        for dest in dests:
            if dest not in rev_graph:
                rev_graph[dest] = []
            rev_graph[dest].append(src)
            
    capture_pipeline = set(capture_nodes)
    queue = list(capture_nodes)
    while queue:
        curr = queue.pop(0)
        for prev in rev_graph.get(curr, []):
            if prev not in capture_pipeline:
                capture_pipeline.add(prev)
                queue.append(prev)
                
    print("Capture pipeline nodes:")
    for n in capture_pipeline:
        print(" -", n)
        
    # Final apps are those that receive from capture_pipeline but are NOT in capture_pipeline
    final_apps = set()
    for src in capture_pipeline:
        for dest in graph.get(src, []):
            if dest not in capture_pipeline and "Dashboard-Soundboard" not in dest:
                final_apps.add(dest)
                
    print("\nFinal Apps:")
    for a in final_apps:
        print(" -", a)

test_route()
