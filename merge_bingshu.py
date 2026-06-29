import json
import os

def merge_bingshu():
    generals_file = 'data/generals.json'
    
    with open(generals_file, 'r', encoding='utf-8') as f:
        generals = json.load(f)
        
    gen_dict = {g['name']: g for g in generals}
    
    total_fixes = 0
    for i in range(4):
        bingshu_file = f'scratch/gen_bingshu_{i}.json'
        if not os.path.exists(bingshu_file):
            print(f"File {bingshu_file} not found. Skipping.")
            continue
            
        try:
            with open(bingshu_file, 'r', encoding='utf-8') as f:
                fixes = json.load(f)
                
            for fix in fixes:
                g_name = fix.get('name')
                avail = fix.get('availableBingshu')
                if g_name and g_name in gen_dict and avail:
                    gen_dict[g_name]['availableBingshu'] = avail
                    total_fixes += 1
        except Exception as e:
            print(f"Error processing {bingshu_file}: {e}")
            
    # Save merged
    with open(generals_file, 'w', encoding='utf-8') as f:
        json.dump(generals, f, ensure_ascii=False, indent=1)
        
    print(f"Successfully applied availableBingshu to {total_fixes} generals.")

if __name__ == '__main__':
    merge_bingshu()
