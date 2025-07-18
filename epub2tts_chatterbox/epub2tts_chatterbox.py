import os
import sys
# Automatically enable MPS fallback on Apple Silicon macOS
# But prob not, see https://github.com/resemble-ai/chatterbox/blob/master/example_for_mac.py
if sys.platform == 'darwin':
    os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
import argparse
import numpy as np
import re
import soundfile
import subprocess
import torch
import warnings
from tqdm import tqdm
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub
import soundfile as sf
from lxml import etree
from mutagen import mp4
import nltk
from nltk.tokenize import sent_tokenize
from PIL import Image
from pydub import AudioSegment
import zipfile
import warnings

warnings.filterwarnings("ignore")

namespaces = {
   "calibre":"http://calibre.kovidgoyal.net/2009/metadata",
   "dc":"http://purl.org/dc/elements/1.1/",
   "dcterms":"http://purl.org/dc/terms/",
   "opf":"http://www.idpf.org/2007/opf",
   "u":"urn:oasis:names:tc:opendocument:xmlns:container",
   "xsi":"http://www.w3.org/2001/XMLSchema-instance",
}

warnings.filterwarnings("ignore", module="ebooklib.epub")

def ensure_punkt():
    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        nltk.download("punkt")
    try:
        nltk.data.find("tokenizers/punkt_tab")
    except LookupError:
        nltk.download("punkt_tab")

def conditional_sentence_case(sent):
    # Split the sentence into words
    words = sent.split()
    length = len(words)
    # Iterate through words to check for three consecutive uppercase words
    for i in range(length - 2):
        if words[i].isupper() and words[i+1].isupper() and words[i+2].isupper():
            # Convert the entire sentence to lowercase and capitalize the first letter
            sent = ' '.join(words).lower().capitalize()
            break  # No need to continue checking once a match is found
    return sent

def chap2text_epub(chap, item_id=None, toc=None):
    """
    Extract chapter title and paragraphs from an EPUB chapter.
    
    Args:
        chap: The chapter content (HTML).
        item_id: The ID of the item in the EPUB spine (for fallback naming).
        toc: The EPUB's table of contents (for fallback title extraction).
    
    Returns:
        tuple: (chapter_title_text, paragraphs)
    """
    blacklist = [
        "[document]",
        "noscript",
        "header",
        "html",
        "meta",
        "head",
        "input",
        "script",
    ]
    paragraphs = []
    soup = BeautifulSoup(chap, "html.parser")

    # Step 1: Try to find chapter title in heading tags (<h1>, <h2>, <h3>)
    heading_tags = ['h1', 'h2', 'h3']
    chapter_title_text = None
    for tag in heading_tags:
        heading = soup.find(tag)
        if heading and heading.text.strip():
            chapter_title_text = heading.text.strip()
            print(f"Found title in <{tag}>: '{chapter_title_text}'")
            break

    # Step 2: If no heading found, try elements with common class names
    if not chapter_title_text:
        common_classes = ['chapter', 'chapter-title', 'title', 'heading']
        for class_name in common_classes:
            element = soup.find(class_=class_name)
            if element and element.text.strip():
                chapter_title_text = element.text.strip()
                print(f"Found title in class '{class_name}': '{chapter_title_text}'")
                break

    # Step 3: Fallback to TOC if provided
    if not chapter_title_text and toc and item_id:
        for toc_item in toc:
            if toc_item.href.split('#')[0] == item_id:
                chapter_title_text = toc_item.title
                print(f"Found title in TOC for item '{item_id}': '{chapter_title_text}'")
                break

    # Step 4: Fallback to item ID or generic name
    if not chapter_title_text:
        chapter_title_text = item_id.replace('.xhtml', '').replace('_', ' ').title() if item_id else None
        print(f"No title found, using fallback: '{chapter_title_text}'")

    # Remove footnotes (links with only numbers)
    for a in soup.findAll("a", href=True):
        if not any(char.isalpha() for char in a.text):
            a.extract()

    # Remove superscript numbers (e.g., footnote markers)
    for sup in soup.findAll("sup"):
        if sup.text.isdigit():
            sup.extract()

    # Extract paragraphs
    chapter_paragraphs = soup.find_all("p")
    if not chapter_paragraphs:
        print(f"No <p> tags found in '{chapter_title_text or item_id}'. Trying <div>.")
        chapter_paragraphs = soup.find_all("div")

    for p in chapter_paragraphs:
        paragraph_text = "".join(p.strings).strip()
        if paragraph_text:
            paragraphs.append(paragraph_text)

    return chapter_title_text, paragraphs

def get_epub_cover(epub_path):
    try:
        with zipfile.ZipFile(epub_path) as z:
            t = etree.fromstring(z.read("META-INF/container.xml"))
            rootfile_path =  t.xpath("/u:container/u:rootfiles/u:rootfile",
                                        namespaces=namespaces)[0].get("full-path")

            t = etree.fromstring(z.read(rootfile_path))
            cover_meta = t.xpath("//opf:metadata/opf:meta[@name='cover']",
                                        namespaces=namespaces)
            if not cover_meta:
                print("No cover image found.")
                return None
            cover_id = cover_meta[0].get("content")

            cover_item = t.xpath("//opf:manifest/opf:item[@id='" + cover_id + "']",
                                            namespaces=namespaces)
            if not cover_item:
                print("No cover image found.")
                return None
            cover_href = cover_item[0].get("href")
            cover_path = os.path.join(os.path.dirname(rootfile_path), cover_href)
            if os.name == 'nt' and '\\' in cover_path:
                cover_path = cover_path.replace("\\", "/")
            return z.open(cover_path)
    except FileNotFoundError:
        print(f"Could not get cover image of {epub_path}")

def export(book, sourcefile):
    book_contents = []
    cover_image = get_epub_cover(sourcefile)
    image_path = None

    if cover_image is not None:
        image = Image.open(cover_image)
        image_filename = sourcefile.replace(".epub", ".png")
        image_path = os.path.join(image_filename)
        image.save(image_path)
        print(f"Cover image saved to {image_path}")

    # Get the table of contents
    toc = book.get_toc() if hasattr(book, 'get_toc') else []

    spine_ids = [spine_tuple[0] for spine_tuple in book.spine if spine_tuple[1] == 'yes']
    items = {item.get_id(): item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT}

    for id in spine_ids:
        item = items.get(id)
        if item is None:
            continue
        # Pass item_id and toc to chap2text_epub
        chapter_title, chapter_paragraphs = chap2text_epub(item.get_content(), item_id=id, toc=toc)
        book_contents.append({"title": chapter_title, "paragraphs": chapter_paragraphs})

    outfile = sourcefile.replace(".epub", ".txt")
    check_for_file(outfile)
    print(f"Exporting {sourcefile} to {outfile}")
    author = book.get_metadata("DC", "creator")[0][0]
    booktitle = book.get_metadata("DC", "title")[0][0]

    with open(outfile, "w", encoding='utf-8') as file:
        file.write(f"Title: {booktitle}\n")
        file.write(f"Author: {author}\n\n")
        file.write(f"# Title\n")
        file.write(f"{booktitle}, by {author}\n\n")
        for i, chapter in enumerate(book_contents, start=1):
            if not chapter["paragraphs"] or chapter["paragraphs"] == ['']:
                continue
            else:
                # Use chapter title if available, otherwise fallback to "Part {i}"
                title = chapter["title"] if chapter["title"] else f"Part {i}"
                file.write(f"# {title}\n\n")
                for paragraph in chapter["paragraphs"]:
                    clean = re.sub(r'[\s\n]+', ' ', paragraph)
                    clean = re.sub(r'[“”]', '"', clean)  # Curly double quotes to standard double quotes
                    clean = re.sub(r'[‘’]', "'", clean)  # Curly single quotes to standard single quotes
                    clean = re.sub(r'--', ', ', clean)
                    clean = re.sub(r'—', ', ', clean)
                    file.write(f"{clean}\n\n")

    return book_contents

def get_book(sourcefile):
    book_contents = []
    book_title = sourcefile
    book_author = "Unknown"
    chapter_titles = []

    with open(sourcefile, "r", encoding="utf-8") as file:
        current_chapter = {"title": "blank", "paragraphs": []}
        initialized_first_chapter = False
        lines_skipped = 0
        for line in file:

            if lines_skipped < 2 and (line.startswith("Title") or line.startswith("Author")):
                lines_skipped += 1
                if line.startswith('Title: '):
                    book_title = line.replace('Title: ', '').strip()
                elif line.startswith('Author: '):
                    book_author = line.replace('Author: ', '').strip()
                continue

            line = line.strip()
            if line.startswith("#"):
                if current_chapter["paragraphs"] or not initialized_first_chapter:
                    if initialized_first_chapter:
                        book_contents.append(current_chapter)
                    current_chapter = {"title": None, "paragraphs": []}
                    initialized_first_chapter = True
                chapter_title = line[1:].strip()
                if any(c.isalnum() for c in chapter_title):
                    current_chapter["title"] = chapter_title
                    chapter_titles.append(current_chapter["title"])
                else:
                    current_chapter["title"] = "blank"
                    chapter_titles.append("blank")
            elif line:
                if not initialized_first_chapter:
                    chapter_titles.append("blank")
                    initialized_first_chapter = True
                if any(char.isalnum() for char in line):
                    sentences = sent_tokenize(line)
                    cleaned_sentences = [s for s in sentences if any(char.isalnum() for char in s)]
                    line = ' '.join(cleaned_sentences)
                    current_chapter["paragraphs"].append(line)

        # Append the last chapter if it contains any paragraphs.
        if current_chapter["paragraphs"]:
            book_contents.append(current_chapter)

    return book_contents, book_title, book_author, chapter_titles

def sort_key(s):
    # extract number from the string
    return int(re.findall(r'\d+', s)[0])

def check_for_file(filename):
    if os.path.isfile(filename):
        print(f"The file '{filename}' already exists.")
        overwrite = input("Do you want to overwrite the file? (y/n): ")
        if overwrite.lower() != 'y':
            print("Exiting without overwriting the file.")
            sys.exit()
        else:
            os.remove(filename)

def append_silence(tempfile, duration=1200):
    # if temppfile does not exist, return
    if not os.path.isfile(tempfile):
        print(f"File {tempfile} does not exist, skipping silence append.")
        return
    audio = AudioSegment.from_file(tempfile)
    # Create a silence segment
    silence = AudioSegment.silent(duration)
    # Append the silence segment to the audio
    combined = audio + silence
    # Save the combined audio back to file
    combined.export(tempfile, format="flac")

def chatterbox_read(sentences, sample, filenames, model, exaggeration, cfg_weight):
    for i, sent in enumerate(sentences):
        clean_sent = conditional_sentence_case(sent.strip())
        max_attempts = 3
        # This "try 3 times" loop is probably not needed, actual failure was from a torch recursive error that was fixed
        for attempt in range(1, max_attempts + 1):
            try:
                if sample == "none":
                    #print(f"Generating audio for sentence: {clean_sent}")
                    wav = model.generate(clean_sent)
                else:
                    #print(f"Generating audio for sentence: {clean_sent}")
                    # generate(self, text, repetition_penalty=1.2, min_p=0.05, top_p=1.0, audio_prompt_path=None, exaggeration=0.5, cfg_weight=0.5, temperature=0.8)
                    wav = model.generate(clean_sent, audio_prompt_path=sample, exaggeration=exaggeration, cfg_weight=cfg_weight)
                
                #print(f"Saving audio to {filenames[i]}")
                ta.save(filenames[i], wav, model.sr)
                # confirm the file was created
                if not os.path.isfile(filenames[i]):
                    raise FileNotFoundError(f"File {filenames[i]} was not created.")
                break  # Success, exit retry loop

            except Exception as e:
                if attempt < max_attempts:
                    print(f"Attempt {attempt} failed for sentence '{clean_sent}': {e} -- Retrying...")
                else:
                    print(f"Failed to process sentence '{clean_sent}' after {max_attempts} attempts. Error: {e}")

def fix_sentence_length(sentences):
    fixed_sentences = []
    skip_next = False
    for i, sentence in enumerate(sentences):
        if skip_next:
            skip_next = False
            continue
        words = sentence.split()
        if len(words) < 3:
            if i < len(sentences) - 1:
                # Combine with next sentence
                next_sentence = sentences[i + 1]
                combined_sentence = sentence + " " + next_sentence
                fixed_sentences.append(combined_sentence)
                skip_next = True  # Skip the next sentence as it's already combined
            else:
                # Last sentence, combine with previous sentence
                if fixed_sentences:
                    fixed_sentences[-1] += " " + sentence
                else:
                    fixed_sentences.append(sentence)
        else:
            fixed_sentences.append(sentence)
    return fixed_sentences

def read_book(book_contents, sample, notitles, exaggeration, cfg_weight):
    # Automatically detect the best available device
    if torch.cuda.is_available():
        device = "cuda"
    elif torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    current_device = torch.device(device)
    print(f"Attempting to use device: {device}")
    model = ChatterboxTTS.from_pretrained(device=device)

    segments = []
    for i, chapter in enumerate(book_contents, start=1):
        paragraphpause = 600  # default pause between paragraphs in ms
        files = []
        partname = f"part{i}.flac"
        print(f"\n\n")

        if os.path.isfile(partname):
            print(f"{partname} exists, skipping to next chapter")
            segments.append(partname)
        else:
            print(f"Chapter ({i}/{len(book_contents)}): {chapter['title']}\n")
            print(f"Section name: \"{chapter['title']}\"")
            if chapter["title"] == "":
                chapter["title"] = "blank"
            if chapter["title"] != "Title" and notitles != True:
                chapter['paragraphs'][0] = chapter['title'] + ". " + chapter['paragraphs'][0]
            for pindex, paragraph in enumerate(chapter["paragraphs"]):
                ptemp = f"pgraphs{pindex}.flac"
                if os.path.isfile(ptemp):
                    print(f"{ptemp} exists, skipping to next paragraph")
                else:
                    sentences = sent_tokenize(paragraph)
                    # This is probably not needed, commenting out for now
                    # sentences = fix_sentence_length(sentences)
                    filenames = [
                        "sntnc" + str(z) + ".wav" for z in range(len(sentences))
                    ]
                    chatterbox_read(sentences, sample, filenames, model, exaggeration, cfg_weight)
                    append_silence(filenames[-1], paragraphpause)
                    # combine sentences in paragraph
                    sorted_files = sorted(filenames, key=sort_key)
                    #if os.path.exists("sntnc0.wav"):
                    #    sorted_files.insert(0, "sntnc0.wav")
                    combined = AudioSegment.empty()
                    for file in sorted_files:
                        # try/except prob not needed, actual failure was from a torch recursive error that was fixed
                        try:
                            combined += AudioSegment.from_file(file)
                        except:
                            print("FAILURE at sorted file combine")
                            print(f"File: {file}")
                            print(f"sorted files: {sorted_files}")
                            print(f"Unsorted: {filenames}")
                            sys.exit()
                    combined.export(ptemp, format="flac")
                    for file in sorted_files:
                        os.remove(file)
                files.append(ptemp)
            # combine paragraphs into chapter
            append_silence(files[-1], 2000)
            combined = AudioSegment.empty()
            for file in files:
                combined += AudioSegment.from_file(file)
            combined.export(partname, format="flac")
            for file in files:
                os.remove(file)
            segments.append(partname)
    return segments

def generate_metadata(files, author, title, chapter_titles):
    chap = 0
    start_time = 0
    with open("FFMETADATAFILE", "w") as file:
        file.write(";FFMETADATA1\n")
        file.write(f"ARTIST={author}\n")
        file.write(f"ALBUM={title}\n")
        file.write(f"TITLE={title}\n")
        file.write("DESCRIPTION=Made with https://github.com/aedocw/epub2tts-chatterbox\n")
        for file_name in files:
            duration = get_duration(file_name)
            file.write("[CHAPTER]\n")
            file.write("TIMEBASE=1/1000\n")
            file.write(f"START={start_time}\n")
            file.write(f"END={start_time + duration}\n")
            file.write(f"title={chapter_titles[chap]}\n")
            chap += 1
            start_time += duration

def get_duration(file_path):
    audio = AudioSegment.from_file(file_path)
    duration_milliseconds = len(audio)
    return duration_milliseconds

def make_m4b(files, sourcefile, speaker):
    filelist = "filelist.txt"
    speaker_file = os.path.basename(speaker)
    basefile = sourcefile.replace(".txt", "")
    outputm4a = f"{basefile}.m4a"
    outputm4b = f"{basefile} ({speaker_file.split('.wav')[0]}).m4b"
    with open(filelist, "w") as f:
        for filename in files:
            filename = filename.replace("'", "'\\''")
            f.write(f"file '{filename}'\n")
    ffmpeg_command = [
        "ffmpeg",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        filelist,
        "-codec:a",
        "flac",
        "-f",
        "mp4",
        "-strict",
        "-2",
        outputm4a,
    ]
    subprocess.run(ffmpeg_command)
    ffmpeg_command = [
        "ffmpeg",
        "-i",
        outputm4a,
        "-i",
        "FFMETADATAFILE",
        "-map_metadata",
        "1",
        "-codec",
        "aac",
        outputm4b,
    ]
    subprocess.run(ffmpeg_command)
    os.remove(filelist)
    os.remove("FFMETADATAFILE")
    os.remove(outputm4a)
    for f in files:
        os.remove(f)
    return outputm4b

def add_cover(cover_img, filename):
    try:
        if os.path.isfile(cover_img):
            m4b = mp4.MP4(filename)
            cover_image = open(cover_img, "rb").read()
            m4b["covr"] = [mp4.MP4Cover(cover_image)]
            m4b.save()
        else:
            print(f"Cover image {cover_img} not found")
    except:
        print(f"Cover image {cover_img} not found")

def main():
    parser = argparse.ArgumentParser(
        prog="epub2tts-chatterbox",
        description="Read a text file to audiobook format",
    )
    parser.add_argument("sourcefile", type=str, help="The epub or text file to process")
    parser.add_argument(
        "--sample",
        type=str,
        help="Sample wav file to use for voice cloning",
    )
    parser.add_argument(
        "--cover",
        type=str,
        help="jpg image to use for cover",
    )
    parser.add_argument(
        "--notitles",
        action="store_true",
        help="Do not read chapter titles"
    )
    parser.add_argument(
        "--exaggeration",
        type=float,
        default=0.7,
        help="Exaggeration factor for voice cloning (default: 0.7)",
    )
    parser.add_argument(
        "--cfg_weight",
        type=float,
        default=0.4,
        help="CFG weight for voice cloning (default: 0.4)",
    )

    args = parser.parse_args()
    print(args)

    ensure_punkt()

    #If we get an epub, export that to txt file, then exit
    if args.sourcefile.endswith(".epub"):
        book = epub.read_epub(args.sourcefile)
        export(book, args.sourcefile)
        exit()

    book_contents, book_title, book_author, chapter_titles = get_book(args.sourcefile)
    if args.sample is not None:
        sample = args.sample
    else:
        sample = "none"
    files = read_book(book_contents, sample, args.notitles, args.exaggeration, args.cfg_weight)
    generate_metadata(files, book_author, book_title, chapter_titles)
    m4bfilename = make_m4b(files, args.sourcefile, sample)
    add_cover(args.cover, m4bfilename)
    
if __name__ == "__main__":
    main()
