import json
import os
import shutil

def merge_generals():
    generals_file = 'data/generals.json'
    
    # Backup original
    shutil.copy(generals_file, 'data/generals_backup.json')
    
    with open(generals_file, 'r', encoding='utf-8') as f:
        generals = json.load(f)
        
    gen_dict = {g['name']: g for g in generals}
    
    total_fixes = 0
    for i in range(4):
        fixed_file = f'scratch/gen_fixed_{i}.json'
        if not os.path.exists(fixed_file):
            print(f"File {fixed_file} not found. Skipping.")
            continue
            
        try:
            with open(fixed_file, 'r', encoding='utf-8') as f:
                fixes = json.load(f)
                
            for fix in fixes:
                g_name = fix.get('name')
                if g_name and g_name in gen_dict:
                    # Update fields dynamically
                    for k, v in fix.items():
                        if k != 'name':
                            gen_dict[g_name][k] = v
                    total_fixes += 1
        except Exception as e:
            print(f"Error processing {fixed_file}: {e}")
            
    # Save merged
    with open(generals_file, 'w', encoding='utf-8') as f:
        json.dump(generals, f, ensure_ascii=False, indent=1)
        
    print(f"Successfully applied full official stats to {total_fixes} generals.")

if __name__ == '__main__':
    merge_generals()
