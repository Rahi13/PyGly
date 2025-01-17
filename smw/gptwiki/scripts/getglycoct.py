

from getwiki import GPTWiki, Glycan

import findpygly
from pygly.GlyTouCan import GlyTouCan

import os, sys, urllib, string
import Bio.SeqIO

gtc = GlyTouCan(usecache=True)
w = GPTWiki()
try:
    os.mkdir('../glycoct')
except OSError:
    pass

for gc in sorted(w.iterglycans(),key=lambda gc:gc.get('accession')):
    acc = gc.get('accession')
    topos = map(str.strip,map(str,gc.get('topo')))
    for tacc in topos:
        glycoct = gtc.getseq(tacc,'glycoct')
	if not glycoct:
	    gly = gtc.getGlycan(tacc)
	    glycoct = gly.glycoct()
        f = open('../glycoct/' + acc + '.' + tacc + '.txt', 'w')
        f.write(glycoct)
        f.close() 
	print >>sys.stderr, "Dump GlycoCT to %s.%s.txt"%(acc,tacc)
