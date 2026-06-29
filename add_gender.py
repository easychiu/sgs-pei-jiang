import json

females = {
    "SP 步練師", "SP 貂蟬", "SP 黃月英", "何氏", "大喬", "孫尚香", "小喬", 
    "張姬", "張春華", "王元姬", "王異", "甄姬", "祝融夫人", "董白", 
    "蔡文姬", "貂蟬", "鄒氏", "關銀屏", "馬雲祿", "黃月英", "呂玲綺"
}

def add_gender():
    with open('data/generals.json', 'r', encoding='utf-8') as f:
        generals = json.load(f)
        
    for g in generals:
        if g['name'] in females:
            g['gender'] = 'Female'
        else:
            g['gender'] = 'Male'
            
    with open('data/generals.json', 'w', encoding='utf-8') as f:
        json.dump(generals, f, ensure_ascii=False, indent=1)
        
if __name__ == '__main__':
    add_gender()
    print("Gender added successfully!")
