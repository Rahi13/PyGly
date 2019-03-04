#!/bin/env python27

import sys
from getwiki import GlycanDataWiki

w = GlycanDataWiki()

if len(sys.argv) > 1:

  if sys.argv[1] == "-":

    for p in w.iterpages(exclude_categories=['Glycan']):
      print >>sys.stderr, p.name
      w.refresh(p)

  else:

    for p in w.iterpages(regex=sys.argv[1]):
      print >>sys.stderr, p.name
      w.refresh(p)

else:

  for p in w.iterpages(include_categories=['Glycan']):
    print >>sys.stderr, p.name
    w.refresh(p)

