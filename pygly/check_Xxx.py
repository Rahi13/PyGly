import sys

from GlycanFormatter import *
gctf = GlycoCTFormat()

from MonoFormatter import *
iupac = IUPACSym()

from GlyTouCan import *
gtc= GlyTouCan()

accs = set(open('../smw/glycandata/data/Xxx_count.txt').read().split())
Xxx_dic = {}

for acc in accs:
    print acc
    glycan = gtc.getGlycan(acc)
    if glycan == None:
        continue
    for m in glycan.all_nodes():
        try:
            sym = iupac.toStr(m)      
        except:
            continue   
        if sym in ('Man','Gal','Glc','Xyl','Fuc','GlcNAc','GalNAc','NeuAc','NeuGc'):
            continue
        else:
            glycoCTMonoStr = gctf.mtoStr(m)
            print 'sym is Xxx'
            if glycoCTMonoStr in Xxx_dic:
                Xxx_dic[glycoCTMonoStr] += 1
                print Xxx_dic
                continue
            else:
                Xxx_dic[glycoCTMonoStr] = 1
                continue
wh = open('../smw/glycandata/data/Xxx_monos.txt','w')
for k,v in Xxx_dic.items():
    print >> wh,([k,v])
wh.close() 
