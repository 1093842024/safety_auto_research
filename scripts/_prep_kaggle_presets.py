import os, urllib.request, io
import numpy as np, pandas as pd

base = "data/oss/kaggle_presets"
os.makedirs(base, exist_ok=True)

def save(sub, df, note=""):
    d = os.path.join(base, sub); os.makedirs(d, exist_ok=True)
    df.to_csv(os.path.join(d, "train.csv"), index=False)
    print(f"  wrote {sub}/train.csv  rows={len(df)} cols={len(df.columns)} {note}")

def fetch(url, timeout=25):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return pd.read_csv(io.BytesIO(r.read()))

# ---- titanic ----
titanic_urls = [
    "https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv",
    "https://raw.githubusercontent.com/cryptoli/DataSets/master/titanic.csv",
]
df_t = None
for u in titanic_urls:
    try:
        df_t = fetch(u)
        print("titanic fetched from", u, "cols:", list(df_t.columns)[:6], "...")
        break
    except Exception as e:
        print("titanic fetch failed:", u, e)
if df_t is not None:
    # normalise to Kaggle schema the runner expects
    df_t = df_t.rename(columns={"survived":"Survived","pclass":"Pclass","sex":"Sex","age":"Age",
                                "sibsp":"SibSp","parch":"Parch","fare":"Fare","embarked":"Embarked",
                                "cabin":"Cabin","ticket":"Ticket","name":"Name","passengerid":"PassengerId"})
    keep = ["PassengerId","Survived","Pclass","Name","Sex","Age","SibSp","Parch","Ticket","Fare","Cabin","Embarked"]
    keep = [c for c in keep if c in df_t.columns]
    save("titanic", df_t[keep])
else:
    # synthetic schema-correct titanic
    rng = np.random.default_rng(42)
    n = 891
    sex = rng.choice(["male","female"], n)
    pclass = rng.choice([1,2,3], n, p=[0.24,0.21,0.55])
    age = np.round(rng.normal(29,13,n)).clip(0.5,80)
    fare = np.round(rng.exponential(32,n).clip(5,512),2)
    survived = ((sex=="female")*0.7 + (pclass==1)*0.5 - 0.4 + rng.normal(0,0.25,n)).clip(0,1).round().astype(int)
    df = pd.DataFrame({"PassengerId":range(1,n+1),"Survived":survived,"Pclass":pclass,"Name":[f"Passenger {i}" for i in range(1,n+1)],
                      "Sex":sex,"Age":age,"SibSp":rng.integers(0,5,n),"Parch":rng.integers(0,3,n),
                      "Ticket":[f"T{i}" for i in range(1,n+1)],"Fare":fare,"Cabin":[""]*n,"Embarked":rng.choice(["S","C","Q"],n)})
    save("titanic", df, "[SYNTHETIC schema-correct]")

# ---- spaceship (subdir 'spaceship-titanic') ----
space_urls = [
    "https://raw.githubusercontent.com/SUHAS-003/Spaceship-Titanic-Dataset/main/spaceship-titanic/train.csv",
    "https://raw.githubusercontent.com/rashida048/Spaceship-Titanic/main/train.csv",
]
df_s = None
for u in space_urls:
    try:
        df_s = fetch(u)
        print("spaceship fetched from", u, "cols:", list(df_s.columns)[:6], "...")
        break
    except Exception as e:
        print("spaceship fetch failed:", u, e)
if df_s is not None:
    df_s = df_s.rename(columns={"transported":"Transported","homeplanet":"HomePlanet","cryosleep":"CryoSleep",
                                "cabin":"Cabin","destination":"Destination","vip":"VIP","roomservice":"RoomService",
                                "foodcourt":"FoodCourt","shoppingmall":"ShoppingMall","spa":"Spa","vrdeck":"VRDeck",
                                "passengerid":"PassengerId","name":"Name"})
    keep = ["PassengerId","HomePlanet","CryoSleep","Cabin","Destination","Age","VIP","RoomService",
            "FoodCourt","ShoppingMall","Spa","VRDeck","Name","Transported"]
    keep = [c for c in keep if c in df_s.columns]
    save("spaceship-titanic", df_s[keep])
else:
    rng = np.random.default_rng(7)
    n = 4000
    hp = rng.choice(["Earth","Mars","Europa"], n)
    cs = rng.choice(["True","False"], n)
    dest = rng.choice(["TRAPPIST-1e","PSO J318.5-22","55 Cancri e"], n)
    vip = rng.choice(["True","False"], n)
    spend = np.round(rng.exponential(800,n).clip(0,20000),1)
    age = np.round(rng.normal(30,12,n)).clip(1,90)
    transported = ((hp=="Earth")*0.4 + (cs=="True")*0.3 - 0.3 + rng.normal(0,0.2,n)).clip(0,1).round().astype(int)
    df = pd.DataFrame({"PassengerId":[f"{i}_01" for i in range(1,n+1)],"HomePlanet":hp,"CryoSleep":cs,
                       "Cabin":[f"{rng.choice(list('ABCDEFG'))}{rng.integers(0,1000)}" for _ in range(n)],
                       "Destination":dest,"Age":age,"VIP":vip,
                       "RoomService":spend,"FoodCourt":spend,"ShoppingMall":spend,"Spa":spend,"VRDeck":spend,
                       "Name":[f"Guest {i}" for i in range(1,n+1)],"Transported":transported})
    save("spaceship-titanic", df, "[SYNTHETIC schema-correct]")
print("DONE")
