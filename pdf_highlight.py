#! /usr/bin/python
#
# pdf_highlight.py -- command line tool to show search or compare results in a PDF
#
# (c) 2012-2013 Juergen Weigert jw@suse.de
# Distribute under GPL-2.0 or ask
#
# 2012-03-16, V0.1 jw - initial draught: argparse, pdftohtml-xml, font.metrics
# 2012-03-20, V0.2 jw - all is there, but the coordinate systems of my overlay 
#                       does not match. Sigh.
# 2013-01-13, V0.3 jw - support encrypted files added, 
#                       often unencrypted is actually encrypted with key=''
#                     - coordinate transformation from xml to pdf canvas added
#                     - refactored: xml2wordlist, xml2fontinfo, create_mark
#                     - added experimental zap_letter_spacing()
# 2013-01-15, V0.4 jw - added class DecoratedWord, 
#                     - option --compare works!
#
# osc in devel:languages:python python-pypdf >= 1.13+20130112
#  need fix from https://bugs.launchpad.net/pypdf/+bug/242756
# osc in devel:languages:python python-reportlab
# osc in devel:languages:python python-pygame
# osc in X11:common:Factory poppler-tools 
#
# needs module difflib from python-base
#
# Feature request:
# - poppler-tools:/usr/bin/pdftohtml -xml should report a rotation angle, 
#   if text is not left-to-right.
#
# TODO:
# - add baloon popups containing deleted or replaced text!
# - if pagebreaks are within deleted text, point this out in the baloon popup.
# - SequenceMatcher() likes to announce long stretches of text as replaced.
#   Can we tune this, to show more insert and delete?
#   If the replaced text has a low correlation ratio, 
#   we should change the replace mark into a combination of delete and insert.
# - one letter changes always become word changes.
#   Either run in single character mode. Or try to trim the replaced text for 
#   common suffix or common prefix.
#
# - pydoc difflib.SequenceMatcher has this:
#   "See the Differ class for a fancy human-friendly file differencer, which
#    uses SequenceMatcher both to compare sequences of lines, and to compare
#    sequences of characters within similar (near-matching) lines."


__VERSION__ = '0.4'

from cStringIO import StringIO
from pyPdf import PdfFileWriter, PdfFileReader, generic as Pdf
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color

import re
from pprint import pprint
import xml.etree.cElementTree as ET
import sys, os, subprocess
from optparse import OptionParser
from argparse import ArgumentParser
import pygame.font as PGF
from difflib import SequenceMatcher

# allow debug printing into less:
import codecs
sys.stdout = codecs.getwriter('utf8')(sys.stdout)

# from pdfminer.fontmetrics import FONT_METRICS
# FONT_METRICS['Helvetica'][1]['W']
#  944

def paint_page_marks(canvas, mediabox, marks, trans=0.5, cb_x=0.98,cb_w=0.005, min_w=0.01, ext_w=0.05):
  # cb_x=0.98 changebar on right margin
  # cb_x=0.02 changebar on left margin
  # min_w=0.05: each mark is min 5% of the page width wide. If not we add extenders.

  # mediabox [0, 0, 612, 792], list of 4x float or FloatObject
  # FloatObject does not support arithmetics with float. Needs casting. Sigh.
  # marks = { h:1188, w:918, x:0, y:0, rect: [{x,y,w,h,t},...], nr:1 }
  def x2c(x):
    return (0.0+x*float(mediabox[2])/marks['w'])
  def w2c(w):
    return (0.0+w*float(mediabox[2])/marks['w'])
  def y2c(y):
    return (0.0+float(mediabox[3])-y*float(mediabox[3])/marks['h'])
  def h2c(h):
    return (0.0+h*float(mediabox[3])/marks['h'])
  
  cb_x = (cb_x-0.5*cb_w) * marks['w']     # relative to pdf page width 
  cb_w = cb_w            * marks['w']     # relative to pdf page width 
  min_w = min_w          * float(mediabox[2])    # relative to xml page width 
  ext_w = ext_w          * float(mediabox[2])    # extenders, if needed

  debug=False
  canvas.setFont('Helvetica',5)
  ### a testing grid
  if debug:
    for x in range(0,13):
      for y in range(0,50):
        canvas.drawString(50*x,20*y,'.(%d,%d)' % (50*x,20*y))
  if debug: canvas.setFont('Helvetica',16)
  for m in marks['rect']:
    canvas.setFillColor(Color(m['c'][0],m['c'][1],m['c'][2], alpha=trans))
    # m = {'h': 23, 'c': [1,0,1], 't': 'Popular', 'w': 76.56716417910448, 'x': 221.0, 'y': 299}
    (x,y,w,h) = (m['x'], m['y'], m['w'], m['h'])
    if w < min_w:
      if debug: print "min_w:%s (%s)" % (min_w, w)
      canvas.rect(x2c(x-ext_w),y2c(y+0.2*h), w2c(w+2*ext_w),h2c(0.2*h), fill=1, stroke=0)
      canvas.rect(x2c(x-ext_w),y2c(y-1.2*h), w2c(w+2*ext_w),h2c(0.2*h), fill=1, stroke=0)
      x = x - (0.5 * (min_w-w))
      canvas.rect(x2c(x),y2c(y),w2c(min_w),h2c(h*1.2), fill=1, stroke=0)
    else:
      # multiply height h with 1.4 to add some top padding, similar
      # to the bottom padding that is automatically added
      # due to descenders extending across the font baseline.
      # 1.2 is often not enough to look symmetric.
      canvas.rect(x2c(x),y2c(y),    w2c(w),h2c(h*1.4), fill=1, stroke=0)

    # change bar
    canvas.rect(x2c(cb_x),  y2c(y),w2c(cb_w),  h2c(h*1.4), fill=1, stroke=0)
    if debug:
      canvas.drawString(x2c(x),y2c(y),'.(%d,%d)%s(%d,%d)' % (x2c(x),y2c(y),m['t'],x,y))
      pprint(m)
      return      # shortcut, only the first word of the page

def pdf2xml(parser, infile, key=''):
  """ read a pdf file with pdftohtml and parse the resulting xml into a dom tree
      the first parameter, parser is only used for calling exit() with proper messages.
  """
  pdftohtml_cmd = ["pdftohtml", "-i", "-nodrm", "-nomerge", "-stdout", "-xml"]
  if len(key):
    pdftohtml_cmd += ["-upw", key]
  try:
    (to_child, from_child) = os.popen2(pdftohtml_cmd + [infile])
  except Exception,e:
    parser.exit("pdftohtml -xml failed: " + str(e))

  # print from_child.readlines()

  try:
    dom = ET.parse(from_child)
  except Exception,e:
    parser.exit("pdftohtml -xml failed.\nET.parse: " + str(e) + ")\n\n" + parser.format_usage())
  print "pdf2xml done"
  return dom

def txt2wordlist(text, context):
  """returns a list of 4-element lists like this:
     [word, text, idx, context]
     where the word was found in the text string at offset idx.
     words are defined as any printable text delimited by whitespace.
     just as str.split() would do.
     Those 4-element lists are cast into DecoratedWord.
     The DecoratedWord type extends the list type, so that it is hashable and
     comparable using only the "word" which is the first element of the four. 
     Thus our wordlists work well as sequences with difflib, although they also
     transport all the context to compute exact page positions later.
  """
  class DecoratedWord(list):
    def __eq__(a,b):
      return a[0] == b[0]
    def __hash__(self):
      return hash(self[0])

  wl = []
  idx = 0
  tl = re.split("(\s+)", text)
  while True:
    if len(tl)==0: break
    head = tl.pop(0)
    if len(head):
      wl.append(DecoratedWord([head, text, idx, context]))
    if len(tl)==0: break
    sep = tl.pop(0)
    idx += len(sep)+len(head)
  return wl
  
def xml2wordlist(dom, last_page=None):
  """input: a dom tree as generated by pdftohtml -xml.
     output: a wordlist with all the metadata so that the exact coordinates
             of each word can be calculated.
  """
  ## Caution: 
  # <text font="1" height="14" left="230" top="203" width="635">8-bit microcontroller based on the AVR enhanced RISC architecture. By executing powerful</text>
  # <text font="1" height="14" left="230" top="223" width="635">i n s t r u c t i o n s   i n   a   s i n g l e   c l o c k   c y c l e ,   t h e</text>
  ## pdftohtml -xml can return strings where each letter is padded with a whitespace. 
  ## zap_letter_spacing() handles this (somewhat)
  ## Seen in atmega164_324_644_1284_8272S.pdf

  wl=[]
  p_nr = 0
  for p in dom.findall('page'):
    if not last_page is None:
      if p_nr >= int(last_page):
        break
    p_nr += 1

    for e in p.findall('text'):
      # <text font="0" height="19" left="54" top="107" width="87"><b>Features</b></text>
      x=e.attrib['left']
      y=e.attrib['top']
      w=e.attrib['width']
      h=e.attrib['height']
      f=e.attrib['font']
      text = ''
      for t in e.itertext(): text += t
      wl += txt2wordlist(text, {'p':p_nr, 'x':x, 'y':y, 'w':w, 'h':h, 'f':f})
    #pprint(wl)
  print "xml2wordlist: %d pages" % p_nr
  return wl

def xml2fontinfo(dom, last_page=None):
  finfo = [None]      # each page may add (or overwrite?) some fonts
  p_finfo = {}
  p_nr = 0
  for p in dom.findall('page'):
    if not last_page is None:
      if p_nr >= int(last_page):
        break
    p_nr += 1
    p_finfo = p_finfo.copy()
    # print "----------------- page %s -----------------" % p.attrib['number']

    for fspec in p.findall('fontspec'):
      fname = fspec.attrib.get('family', 'Helvetica')
      fsize = fspec.attrib.get('size', 12)
      f_id  = fspec.attrib.get('id')
      f_file = PGF.match_font(fname)
      f = PGF.Font(f_file, int(0.5+float(fsize)))
      p_finfo[f_id] = { 'name': fname, 'size':fsize, 'file': f_file, 'font':f }
    #pprint(p_finfo)
    finfo.append(p_finfo)
  return finfo


def main():
  debug = True
  parser = ArgumentParser(epilog="version: "+__VERSION__, description="highlight words in a PDF file.")
  parser.def_trans = 0.3
  parser.def_decrypt_key = ''
  parser.def_sea_col = ['pink', [1,0,1]]
  parser.def_add_col = ['green',  [0.3,1,0.3]]
  parser.def_del_col = ['red',    [1,.3,.3]]
  parser.def_chg_col = ['yellow', [.8,.8,0]]
  parser.def_output = 'output.pdf'
  parser.add_argument("-o", "--output", metavar="OUTFILE", default=parser.def_output,
                      help="write output to FILE; default: "+parser.def_output)
  parser.add_argument("-s", "--search", metavar="WORD_REGEXP", 
                      help="highlight only WORD_REGEXP")
  parser.add_argument("-d", "--decrypt-key", metavar="DECRYPT_KEY", default=parser.def_decrypt_key,
                      help="open an encrypted PDF; default: KEY='"+parser.def_decrypt_key+"'")
  parser.add_argument("-c", "--compare-text", metavar="OLDFILE",
                      help="mark inserted, deleted and replaced text with regard to OLDFILE. This works word by word.")
  parser.add_argument("-e", "--exclude-irrelevant-pages", default=False, action="store_true",
                      help="with -s: show only matching pages; with -c: show only changed pages; default: reproduce all pages from INFILE in OUTFILE")
  parser.add_argument("-i", "--nocase", default=False, action="store_true",
                      help="make -s case insensitive; default: case sensitive")
  parser.add_argument("-L", "--last-page", metavar="LAST_PAGE",
                      help="limit pages processed; this counts pages, it does not use document page numbers; default: all pages")
  parser.add_argument("-t", "--transparency", type=float, default=parser.def_trans, metavar="TRANSP", 
                      help="set transparency of the highlight; invisible: 0.0; full opaque: 1.0; default: " + str(parser.def_trans))
  parser.add_argument("-C", "--search-color", default=parser.def_sea_col[1], nargs=3, metavar="N",
                      help="set color of the search highlight as an RGB triplet; default is %s: %s" 
                      % (parser.def_sea_col[0], ' '.join(map(lambda x: str(x), parser.def_sea_col[1])))
                      )
  parser.add_argument("infile", metavar="INFILE", help="the input filename")
  args = parser.parse_args()      # --help is automatic

  ## TEST this, fix or disable: they should work well together:
  # if args.search and args.compare_text:
  #   parser.exit("Usage error: -s search and -c compare are mutually exclusive, try --help")

  if args.search is None and args.compare_text is None:
    parser.exit("Oops. Nothing to do. Specify either -s or -c")

  dom1 = pdf2xml(parser, args.infile, args.decrypt_key)
  dom2 = None
  wordlist2 = None
  if args.compare_text:
    dom2 = pdf2xml(parser, args.compare_text, args.decrypt_key)
    wordlist2 = xml2wordlist(dom2, args.last_page)

  if debug:
    dom1.write(args.output + ".1.xml")
    if dom2:
      dom2.write(args.output + ".2.xml")

  PGF.init()
  # This pygame.font module is used to calculate widths of all glyphs
  # for words we need to mark. With this calculation, we can determine 
  # the exact position and length of the marks, if the marked word is 
  # only a substring (which it often is).
  # For complete strings, we get the exact positions and size from pdftohtml -xml.
  # Strings returned by pdftohtml are combinations of multiple PDF text fragments.
  # This is good, as pdftohtml reassembles words and often complete lines in a perfectly 
  # readable way. 
  # The downside of this is, that the width and position calculation may be
  # a bit off, due to uneven word-spacing or letter-spacing in the original PDF text line.
  ####
  # f = PGF.Font(PGF.match_font('Times'), 13))
  # f.metrics("Bernoulli") 
  #  [(0, 8, 0, 9, 9), (0, 7, 0, 6, 6), (-1, 5, 0, 6, 4), (-1, 6, 0, 6, 6), (0, 7, 0, 6, 7), (0, 6, 0, 6, 6), (-1, 3, 0, 9, 3), (-1, 3, 0, 9, 3), (-1, 3, 0, 9, 3)]
  # (minx, maxx, miny, maxy, advance)

  page_marks = pdfhtml_xml_find(dom1, re_pattern=args.search, 
      wordlist=wordlist2,
      nocase=args.nocase,
      last_page=args.last_page,
      ext={'a': {'c':parser.def_add_col[1]},
           'd': {'c':parser.def_del_col[1]},
           'c': {'c':parser.def_chg_col[1]},
           'm': {'c':args.search_color} })

  # pprint(page_marks[0])

  output = PdfFileWriter()
  input1 = PdfFileReader(file(args.infile, "rb"))
  if input1.getIsEncrypted():
    if input1.decrypt(args.decrypt_key):
      if len(args.decrypt_key):
        print "Decrypted using key='%s'." % args.decrypt_key
    else:
      parser.exit("decrypt(key='%s') failed." % args.decrypt_key)

  # Evil hack: there is no sane way to transport DocumentInfo metadata.
  #          : This is the insane way, we duplicate this code from
  #          : PdfFileWriter.__init__()
  # FIXME: We should also copy the XMP metadata from the document.

  try:
    di = input1.getDocumentInfo()
    output._objects.append(di)
  except Exception,e:
    print("WARNING: getDocumentInfo() failed: " + str(e) );

  output._info = Pdf.IndirectObject(len(output._objects), 0, output)

  pages_written = 0
  last_page = input1.getNumPages()
  if args.last_page and int(args.last_page) < last_page:
    last_page = int(args.last_page)

  print "input pages: %d" % last_page

  for i in range(0,last_page):
    if len(page_marks[i]['rect']) == 0:
      continue
    print " page %d: %d hits" % (page_marks[i]['nr'], len(page_marks[i]['rect']))

    page = input1.getPage(i)
    box = page['/MediaBox']     # landscape look like [0, 0, 794, 595]

    ## create a canvas of correct size, 
    ## paint semi-transparent highlights on the canvas,
    ## then save the canvas to memory string as proper PDF,
    ## merge this string ontop of the original page.
    pdf_str = StringIO()
    c = canvas.Canvas(pdf_str, pagesize=(box[2],box[3]))
    paint_page_marks(c, box, page_marks[i], trans=args.transparency)

    c.save()
    pdf_str.seek(0,0)
    input2 = PdfFileReader(pdf_str)
    highlight_page = input2.getPage(0)
    if 0:
      ## can paint below document.
      ## this looks better, as the fonts are true black, 
      ## but fails completely, if white background is drawn.
      highlight_page.mergePage(page)
      output.addPage(highlight_page)
    else:
      page.mergePage(highlight_page)
      output.addPage(page)
    pages_written += 1

  outputStream = file(args.output, "wb")
  output.write(outputStream)
  outputStream.close()
  print "%s (%s pages) written." % (args.output, pages_written)



def rendered_text_width(str, font=None):
  """Returns the width of str, in font units.
     If font is not specified, then len(str) is returned.
     """
  if (font is None): return len(str)
  if (len(str) == 0): return 0
  return sum(map(lambda x: x[4], font.metrics(str)))

def rendered_text_pos(string1, char_start, char_count, font=None, xoff=0, width=None):
  """Returns a tuple (xoff2,width2) where substr(string1, ch_start, ch_count) will be rendered
     in relation to string1 being rendered starting at xoff, and being width units wide.

     If font is specified, it is expected to have a metrics() method returning a tuple, where 
     the 5th element is the character width, e.g. a pygame.font.Font().
     Otherwise a monospace font is asumed, where all characters have width 1.

     If width is specified, it is used to recalculate positions so that the entire string1 fits in width.
     Otherwise the values calculated by summing up font metrics by character are used directly.
     """
  pre = string1[:char_start]
  str = string1[char_start:char_start+char_count]
  suf = string1[char_start+char_count:]
  pre_w = rendered_text_width(pre, font)
  str_w = rendered_text_width(str, font)
  suf_w = rendered_text_width(suf, font)
  ratio = 1

  if (width is not None): 
    tot_w = pre_w+str_w+suf_w
    if (tot_w == 0): tot_w = 1
    ratio = float(width)/tot_w
  #pprint([[pre,str,suf,width],[pre_w,str_w,suf_w,tot_w],ratio])
  return (xoff+pre_w*ratio, str_w*ratio)

def create_mark(text,offset,length, font, t_x, t_y, t_w, t_h, ext={}):
  #print "word: at %d is '%s'" % (offset, text[offset:offset+length]),
  (xoff,width) = rendered_text_pos(text, offset, length,
                          font, float(t_x), float(t_w))
  #print "  xoff=%.1f, width=%.1f" % (xoff, width)
  mark = {'x':xoff, 'y':float(t_y)+float(t_h),
          'w':width, 'h':float(t_h), 't':text[offset:offset+length]}
  for k in ext:
    mark[k] = ext[k]
  return mark

def zap_letter_spacing(text):
  ###
  # <text font="1" height="14" left="230" top="223" width="635">i n s t r u c t i o n s   i n   a   s i n g l e   c l o c k   c y c l e ,   t h e</text>
  # Does not normally match a word. But xmltohtml -xml returns such approximate renderings for block justified texts.
  # Sigh. One would need to search for c\s*l\s*o\s*c\s*k to find clock there.
  # if every second letter of a string is a whitespace, then remove these extra whitespaces.
  ###
  l = text.split(' ')
  maxw = 0
  for w in l:
    if len(w) > maxw: maxw = len(w)
  if maxw > 1: return text

  # found whitespaces padding as seen in the above example.
  # "f o o   b a r ".split(' ')
  # ['f', 'o', 'o', '', '', 'b', 'a', 'r', '']
  t = ''
  for w in l:
    if len(w) == 0: w = ' '
    t += w
  #print "zap_letter_spacing('%s') -> '%s'" % (text,t)
  return t


def pdfhtml_xml_find(dom, re_pattern=None, wordlist=None, nocase=False, ext={}, last_page=None):
  """traverse the XML dom tree, (which is expected to come from pdf2html -xml)
     find all occurances of re_pattern on all pages, returning rect list for 
     each page, giving the exact coordinates of the bounding box of all 
     occurances. Font metrics are used to interpolate into the line fragments 
     found in the dom tree.
     Keys and values from ext['m'] are merged into the DecoratedWord output for pattern matches.
     If re_pattern is None, then wordlist is used instead. 
     Keys and values from ext['a'], ext['d'], or ext['c'] respectively are merged into 
     the DecoratedWord output for added, deleted, or changed texts (respectivly).
  """
  fontinfo = xml2fontinfo(dom, last_page)

  def catwords(dw, idx1, idx2):
    text = " ".join(map(lambda x: x[0], dw[idx1:idx2]))
    start = "p%s%s" % (dw[idx1][3].get('p','?'),dw[idx1][3].get('o',''))
    return [text,start]

  p_rect_dict = {}   # indexed by page numbers, then lists of marks
  if wordlist:
    # generate our wordlist too, so that we can diff against the given wordlist.
    wl_new = xml2wordlist(dom, last_page)
    s = SequenceMatcher(None, wordlist, wl_new, autojunk=False)
    for tag, i1, i2, j1, j2 in s.get_opcodes():
      if tag == "equal":
        continue
      elif tag == "replace":
        attr = ext['c'].copy()
        attr['o'] = catwords(wordlist, i1, i2)
      elif tag == "delete":
        attr = ext['d'].copy()
        attr['o'] = catwords(wordlist, i1, i2)
        j2 = j1 + 1     # so that the create_mark loop below executes once.
      elif tag == "insert":
        attr = ext['a'].copy()
      else:
        print "SequenceMatcher returned unknown tag: %s" % tag
        continue
      attr['t'] = tag
      # print "len(wl_new)=%d, j in [%d:%d] %s" % (len(wl_new), j1, j2,tag)
      for j in range(j1,j2):
        if j >= len(wl_new):    # this happens with autojunk=False!
          print "end of wordlist reached: %d" % j
          break
        w = wl_new[j]
        p_nr = w[3].get('p','?')
        l = len(w[0])
        if tag == 'delete': l = 0 # very small marker length triggers extenders.
        mark = create_mark(w[1], w[2], l,
                fontinfo[p_nr][w[3]['f']]['font'], 
                w[3]['x'],w[3]['y'],w[3]['w'],w[3]['h'], attr)
        if not p_rect_dict.has_key(p_nr): p_rect_dict[p_nr] = []
        p_rect_dict[p_nr].append(mark)

  # End of wordlist code.
  # We have now p_rect_dict preloaded with the wordlist marks or empty.
  # next loop through all pages, select the correct p_rect from the dict.
  # Start or continue adding re_pattern search results while if any.
  # Finally collect all in pages_a.in pages_a.
  pages_a = []
  p_nr = 0
  for p in dom.findall('page'):
    if not last_page is None:
      if p_nr >= int(last_page):
        break
    p_nr += 1

    p_rect = p_rect_dict.get(p_nr,[])
    if re_pattern:
      for e in p.findall('text'):
        p_finfo = fontinfo[p_nr]
        text = ''
        for t in e.itertext(): text += t
        text = zap_letter_spacing(text)
  
        #pprint([e.attrib, text])
        #print "search (%s)" % re_pattern
        flags = re.UNICODE
        if (nocase): flags |= re.IGNORECASE
        l = map(lambda x:len(x), re.split('('+re_pattern+')', text, flags=flags))
        l.append(0)       # dummy to make an even number.
        # all odd indices in l are word lengths, all even ones are seperator lengths
        offset = 0
        i = 0
        while (i < len(l)):
          # print "offset=%d, i=%d, l=%s" % (offset, i, repr(l))
          offset += l[i]
          if (l[i+1] > 0):
  
            p_rect.append(create_mark(text,offset,l[i+1], 
              p_finfo[e.attrib['font']]['font'], 
              e.attrib['left'], e.attrib['top'], 
              e.attrib['width'],e.attrib['height'], ext['m']))
    
            offset += l[i+1]
          i += 2
    pages_a.append({'nr':int(p.attrib['number']), 'rect':p_rect,
                 'h':float(p.attrib['height']), 'w':float(p.attrib['width']),
                 'x':float(p.attrib['left']), 'y':float(p.attrib['top'])})
  return pages_a

if __name__ == "__main__": main()
