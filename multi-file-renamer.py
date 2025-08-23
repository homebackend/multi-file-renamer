"""Rename multiple files"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
from queue import Queue
from typing import Any, Dict, List

import spacy
import yaml
from dateutil import parser
from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from spacy.language import Language
from spacy.matcher import Matcher
from spacy.tokens import Doc, Span, DocBin
from spacy.util import filter_spans

TRAIN_DATA_PATH = 'train_data.spacy'
TRAIN_DATA_DEV_PATH = 'train_data_dev.spacy'
DETAILS_PATH = 'details.json'
PATTERNS_PATH = 'patterns.yaml'
FILE_NAMES_PATH = 'file_names.json'
RESTORE_PATH = 'restore_data.json'

nlp_data = {
    "nlp": None,
    "matcher": None,
    "patterns": {}
}


def get_actual_value(span: Span):
    """
    Get value for the span for use during file renaming

    Args:
        span (Span): span for which value is to be retrieved

    Returns:
        dict: The calculated value of the field(s) as per the configured pattern
    """

    if span.label_ not in nlp_data["patterns"]:
        return {}

    input_rules = nlp_data["patterns"][span.label_].get("input", {})
    output_rules = nlp_data["patterns"][span.label_].get("output", {})

    value = get_input_value(span, input_rules)
    return process_output(value, output_rules)


@Language.component("rename_pipe")
def rename_pipe_entity(doc: Doc):
    """
    Add matched entities to the document. This function is used in spacy pipeline.

    Args:
        doc (Doc): spacy document object

    Returns:
        Doc: doc object returned for use in pipeline
    """

    matches = nlp_data["matcher"](doc)

    ents = []
    for match_id, start, end in matches:
        ents.append(
            Span(doc, start, end, label=nlp_data["nlp"].vocab.strings[match_id]))

    doc.ents = filter_spans(ents)

    return doc


def load_patterns(file_path: str):
    """
    Loads patterns.yaml or equivalent file.

    Args:
        file_path (str): file path to load patterns from

    Returns:
        None
    """

    try:
        env = Environment(loader=FileSystemLoader('.'))
        template = env.get_template(file_path)
        yaml_string = template.render({})

        nlp_data["patterns"] = yaml.safe_load(yaml_string)
    except (TemplateNotFound, yaml.YAMLError, spacy.errors.MatchPatternError) as e:
        print(f"Error while reading patterns: {e}")
        sys.exit(1)


def get_matcher(nlp, file_path: str):
    """
    Get matcher for use in spacy pipeline.
    Matcher is using patterns defined in file_path.

    Args:
        nlp: Spacy nlp object
        file_path (str): the file path to load patterns from

    Returns:
        Matcher: matcher object
    """

    matcher = Matcher(nlp.vocab)

    load_patterns(file_path)

    for key, data in nlp_data["patterns"].items():
        matcher.add(key, data["patterns"])

    return matcher


def nlp_init(file_path: str):
    """
    Initializes nlp_data global object

    Args:
        file_path (str): file path to load patterns from

    Returns:
        None
    """

    nlp_data["nlp"] = nlp = spacy.blank("en")
    nlp_data["matcher"] = get_matcher(nlp, file_path)
    nlp.add_pipe("rename_pipe", last=True)

    Span.set_extension("actual_value", getter=get_actual_value)
    # print(f'Prefixes: {nlp.Defaults.prefixes}, Suffixes: {nlp.Defaults.suffixes}, Infixes: {nlp.Defaults.infixes}')


def get_input_value(span: Span, input_rules):
    """
    Get value from given span using specified input rules.

    Args:
        span (Span): Spacy Span object
        input_rules (dict): A dict object containing input rules.
                            Expected keys are:
                            - "type" (one of "single", "all", "distinct" or "multi") 

    Returns:
        object
    """

    if "type" not in input_rules:
        print(":: type is mandatory field for input rules")
        sys.exit(1)

    input_type = input_rules["type"]

    if input_type == "single":
        if "index" not in input_rules:
            print(":: index is mandatory field")
            sys.exit(1)

        index = input_rules["index"]

        if index == "start":
            return span.doc[span.start].text
        if index == "end":
            return span.doc[span.end - 1].text

        if not isinstance(index, int):
            print(":: index is non numeric")
            sys.exit(1)

        return span.doc[index].text
    if input_type == "all":
        return span.text
    if input_type == "distinct":
        if "indexes" not in input_rules:
            print(":: indexes field is mandatory for input type distinct")
            sys.exit(1)

        result = []
        for index in input_rules["indexes"]:
            if isinstance(index, int):
                if index < 0:
                    index += span.end
                else:
                    index += span.start
            else:
                if index == "start":
                    index = span.start
                elif index == "end":
                    index = span.end - 1
                else:
                    print(f":: index keyword {index} is not supported")
                    sys.exit(1)

                result.append(span.doc[index].text)
        return result

    if input_type == "multi":
        if "start" not in input_rules or input_rules["start"] == "start":
            start = span.start
        else:
            if not isinstance(input_rules["start"], int):
                print(":: start is non numeric")
                sys.exit(1)
            start = span.start + int(input_rules["start"])

        if "end" not in input_rules or input_rules["end"] == "end":
            end = span.end
        else:
            if not isinstance(input_rules["end"], int):
                print(":: end is non numeric")
                sys.exit(1)
            end = span.start + int(input_rules["end"])

        return [span.doc[i].text for i in range(start, end)]

    print(f":: Unsupported input type: {input_type}")
    sys.exit(1)


def convert_roman_nums_handler(value: str):
    if value.isnumeric():
        return value

    return str(roman_to_int(value.upper()))


def date_handler(value: str, format: str):
    try:
        p = parser.parse(value)
        return p.strftime(format)
    except parser.ParserError as e:
        print(f":: Invalid date '{value}': {e}")
        sys.exit(1)


def joiner_handler(values: List[Any], separator: str,
                   exclusions: List[Any] = None, outputs: List[Any] = None):
    if outputs is None:
        values = [v for v in values if exclusions is None or v not in exclusions]
        return separator.join(map(str, values))

    if len(values) != len(outputs):
        print(":: multi output handling: count of values and outputs do not match")
        sys.exit(1)

    return separator.join([get_value(values[i], outputs[i]) for i in range(len(values))])


def get_value(value, output: Dict[str, Any]):
    if 'handler' not in output:
        return value

    handlers = {
        "convert_roman_nums": convert_roman_nums_handler,
        "date": date_handler,
        "joiner": joiner_handler,
    }
    handler = output["handler"]

    if handler not in handlers:
        print(f":: Handler {handler} is not implemented")
        sys.exit(1)

    return handlers[handler](value, **output.get("args", {}))


def process_single_output(value, output: Dict[str, Any]):
    if "index" not in output:
        print(f":: index not defined for {output}")
        sys.exit(1)

    index = output["index"]
    return {index: get_value(value, output)}


def process_multi_output(values: List[Any], outputs: List[Any]):
    if not isinstance(outputs, (list, tuple)) \
            and (isinstance(values, (list, tuple)) or len(values) != len(outputs)):
        print(":: multi output handling: count of values and outputs do not match")
        sys.exit(1)

    if not isinstance(values, (list, tuple)):
        values = [values] * len(outputs)

    result = {}

    for i, value in enumerate(values):
        output = outputs[i]

        if "index" not in output:
            print(f":: index not defined for {output}")
            sys.exit(1)

        index = output["index"]
        value = get_value(value, output)
        result[index] = value

    return result


def process_output(value, output: Dict[str, Any]):
    if "type" not in output:
        print(":: Output 'type' not specified")
        sys.exit(1)

    if output["type"] == "single":
        return process_single_output(value, output)

    if output["type"] == "multi":
        if "outputs" not in output:
            print(":: outputs is mandatory for multi output type")
            sys.exit(1)

        return process_multi_output(value, output["outputs"])


def roman_to_int(s: str) -> int:
    """
    Converts a Roman numeral string to its integer equivalent.

    Args:
        s: The Roman numeral string (e.g., "IV", "IX", "MCMXCIV").

    Returns:
        The integer representation of the Roman numeral.
    """
    roman_values = {
        'I': 1, 'V': 5, 'X': 10, 'L': 50,
        'C': 100, 'D': 500, 'M': 1000
    }

    total = 0
    for i, x in enumerate(s):
        current_value = roman_values[x]

        if i + 1 < len(s) and current_value < roman_values[s[i+1]]:
            current_value *= -1

        total += current_value

    return total


def get_title_page_details(pdf_path, page_number, nlp):
    import pytesseract

    images = convert_from_path(
        pdf_path, first_page=page_number, last_page=page_number, dpi=100)
    image_data = images[0]
    if image_data is None:
        return "Error: Could not extract image features."

    text = pytesseract.image_to_string(image_data)
    # print(f'{pdf_path}:{page_number}:: {text}')
    # doc = nlp(text)
    # for ent in doc.ents:
    #    print(f"Entity: {ent.text}, Label: {ent.label_}")

    result = {
        'page': page_number,
    }
    for t in text.split('\n'):
        res = re.search(
            r'.*Vol\s+(?P<volume>[^\s]*)\s+Nos?\s+(?P<nos>(?:(?:\s*and\s*)?(?:\d+))+)\s*(?P<date>.*)', t)
        if res:
            result['match'] = t
            result['volume'] = roman_to_int(
                res.group('volume').upper()) if res.group('volume') is not None else 0
            result['nos'] = [int(i)
                             for i in re.findall(r'(\d+)', res.group('nos'))]
            entities = nlp(res.group('date'))
            for e in entities.ents:
                if e.label_ == "DATE":
                    result['date'] = e.text

    return result


async def detect_title_page(args, result):
    """Detect title page"""

    import contextualSpellCheck
    import spacy

    nlp = spacy.load('en_core_web_sm')
    contextualSpellCheck.add_to_pipe(nlp)
    results = {}
    for pdf_path, titles in result.items():
        results[pdf_path] = []
        for title in titles:
            result = get_title_page_details(pdf_path, title, nlp)
            results[pdf_path].append(result)
    with open(args.save_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=4)


def preprocess_file_name(file_name: str, strips: List[str]):
    processed_line = file_name
    if strips is not None:
        for s in strips:
            processed_line = processed_line.replace(s, '')

    processed_line = re.sub(
        r'\s+', ' ', re.sub(r'([-:.,()\[\]\{\}])', r' \1 ', processed_line.strip()))

    return processed_line


def get_doc(file_name: str, strips: List[str]):
    """Get spacy Doc object for given file"""

    nlp: Language = nlp_data["nlp"]
    processed_line = preprocess_file_name(file_name, strips)
    return nlp(processed_line)


def get_new_file_name(doc: Doc, mandatory: List[str], template_str: str):
    env = Environment()
    template = env.from_string(template_str)

    results = {}

    for e in doc.ents:
        if e.label_ not in nlp_data["patterns"]:
            print(f":: {e.label_} is not supported")
            sys.exit(1)

        result = e._.actual_value
        results = {**results, **result}

    try:
        if mandatory is not None and not all(m in results for m in mandatory):
            return None
        rendered_output = template.render(**results)
        # print(file_name, ' -> ',
        #      [f'{d.text}' for d in doc], ' => ', rendered_output)
        return rendered_output
    except Exception as e:
        print(e)
        sys.exit(1)


def extract(file_name: str, strips: List[str], mandatory: List[str], template_str: str):
    """Extracts new file names"""

    doc = get_doc(file_name, strips)
    return get_new_file_name(doc, mandatory, template_str)


def file_generator(files):
    """Yields file to be processed"""

    file_paths = Queue()
    for f in files:
        file_paths.put(f)

    while not file_paths.empty():
        file_path = file_paths.get()
        if os.path.isdir(file_path):
            listing = os.listdir(file_path)
            for l in listing:
                file_paths.put(f"{file_path}/{l}")
            continue

        dir_name = os.path.dirname(file_path)
        file_name = os.path.basename(file_path)

        yield dir_name, file_name


def multi_extract(args):
    """Extracts new file names"""

    results = {}
    nlp_init(args.load)

    for dir_name, file_name in file_generator(args.file):
        file_name_new = extract(
            file_name, args.excludes, args.mandatory, args.template)
        result = results.get(dir_name, {})
        result[file_name] = file_name_new
        results[dir_name] = result

    return results


def multi_predict(args):
    """Predict new file names"""
    results = {}
    nlp = spacy.load(args.model)
    load_patterns(args.load)
    Span.set_extension("actual_value", getter=get_actual_value)

    for dir_name, file_name in file_generator(args.file):
        doc = nlp(preprocess_file_name(file_name, None))
        file_name_new = get_new_file_name(doc, args.mandatory, args.template)
        result = results.get(dir_name, {})
        result[file_name] = file_name_new
        results[dir_name] = result

    return results


def generate_training_data(args):
    """Generate training data"""

    nlp_init(args.load)
    docs = []

    for _, file_name in file_generator(args.file):
        doc = get_doc(file_name, args.excludes)
        docs.append(doc)

    random.shuffle(docs)
    split = (len(docs) * args.percentage) // 100
    train_docs = docs[:split]
    test_docs = docs[split:]

    return train_docs, test_docs


def rename_file(original_path: str, new_name: str):
    """Rename a file - if a file with new_name already exists a counter is used to generate unique name"""

    original_dir, _ = os.path.split(original_path)
    original_base_name, original_ext = os.path.splitext(new_name)

    counter = 1
    target_path = os.path.join(original_dir, new_name)

    while os.path.exists(target_path):
        # Construct new name with suffix
        suffixed_name = f"{original_base_name}-{counter}{original_ext}"
        target_path = os.path.join(original_dir, suffixed_name)
        counter += 1

    try:
        # print(f"rename({original_path}, {target_path})")
        os.rename(original_path, target_path)
    except OSError as e:
        print(f"Error renaming file: {e}")
        return None

    return target_path


def rename_files(file_data):
    """Rename multiple files"""

    skipped_files = []
    renamed_files = {}
    failed_files = []

    for original_dir, file_mappings in file_data.items():
        for original_name, new_name in file_mappings.items():
            original_file_path = os.path.join(original_dir, original_name)
            if new_name is None:
                skipped_files.append(original_file_path)
            else:
                actual_new_path = rename_file(original_file_path, new_name)
                if actual_new_path is None:
                    failed_files.append(original_file_path)
                else:
                    renamed_files[actual_new_path] = {
                        "original_path": original_file_path,
                        "proposed_name": new_name,
                        "proposed_is_different": os.path.basename(actual_new_path) != new_name
                    }

    return renamed_files, skipped_files, failed_files


def add_generate_command(commands):
    """Add generate command"""
    generate_cmd = commands.add_parser(
        'generate', help='Generate training data')
    add_common_extract_arguments(generate_cmd)
    generate_cmd.add_argument('--percentage', type=int, default=75,
                              help='Percentage of training data to use for training (default: 75)')
    generate_cmd.add_argument('--training-save-path', type=str, default=TRAIN_DATA_PATH,
                              help=f'Save path for training data (default: {TRAIN_DATA_PATH})')
    generate_cmd.add_argument('--testing-save-path', type=str, default=TRAIN_DATA_DEV_PATH,
                              help=f'Save path for test data (default: {TRAIN_DATA_DEV_PATH})')


def add_predict_arguments(predict_cmd, save_path):
    predict_cmd.add_argument('--model', type=str, required=True,
                             help='Model path to use to predict new file names')
    add_extract_arguments(predict_cmd, save_path)


def add_predict_command(commands):
    """Add predict command"""

    predict_cmd = commands.add_parser(
        'predict', help='Predict new file name based on model')
    add_predict_arguments(predict_cmd, FILE_NAMES_PATH)


def add_common_extract_arguments(cmd):
    """Add common arguments"""

    cmd.add_argument('-l', '--load', type=str, default=PATTERNS_PATH,
                     help=f'File to load patterns from (default: {PATTERNS_PATH})')
    cmd.add_argument('--excludes', type=str, nargs='+', default=None,
                     help='Strings that should be excluded from input file names during processing (default: none)')
    cmd.add_argument('file', type=str, nargs='+',
                     help='File of directory to process')


def add_extract_arguments(cmd, save_path=None):
    """Add extract command arguments"""

    if save_path is not None:
        cmd.add_argument('-s', '--save-path', type=str, default=save_path,
                         help=f'Save path (default: {save_path})')
    cmd.add_argument('-m', '--mandatory', type=str, nargs='+', default=None,
                     help='Fields that are mandatory in original file name (default: none)')
    cmd.add_argument('-t', '--template', type=str, required=True,
                     help='template to be used to rename files. Use {attrib_name} for placeholders')
    add_common_extract_arguments(cmd)


def add_extract_command(commands):
    """Add extract command"""

    predict_cmd = commands.add_parser(
        'extract', help='Extract new file names for given files based on specified rules')
    add_extract_arguments(predict_cmd, FILE_NAMES_PATH)


def add_rename_arguments(cmd):
    cmd.add_argument('-s', '--save-path', type=str, default=RESTORE_PATH,
                     help=f'Save path (default: {RESTORE_PATH})')


def add_rename_command(commands):
    """Add rename command"""

    rename_cmd = commands.add_parser(
        'rename', help='Rename files to new names')
    rename_commands = rename_cmd.add_subparsers(
        dest='sub_command', help='Available sub commands')

    run_cmd = rename_commands.add_parser(
        'extract', help='Extract new file names and rename files')
    add_extract_arguments(run_cmd)
    add_rename_arguments(run_cmd)

    predict_cmd = rename_commands.add_parser(
        'predict', help='Predict new file names and rename files')
    add_predict_arguments(predict_cmd, None)
    add_rename_arguments(predict_cmd)

    from_cmd = rename_commands.add_parser(
        'from', help='Load saved data from file and rename files')
    from_cmd.add_argument('-l', '--load-from-file', type=str, default=FILE_NAMES_PATH,
                          help=f'Load title pages and pdf from file (default: {FILE_NAMES_PATH})')
    add_rename_arguments(from_cmd)


def parse_args():
    """Parses command line arguments"""

    parser = argparse.ArgumentParser(description='multi file renamer')

    commands = parser.add_subparsers(
        dest='command', help='Available commands', required=True)
    add_generate_command(commands)
    add_extract_command(commands)
    add_predict_command(commands)
    add_rename_command(commands)

    return parser.parse_args()


async def main():
    """The main function"""

    args = parse_args()

    if args.command == "generate":
        train_docs, test_docs = generate_training_data(args)

        doc_bin = DocBin(docs=train_docs)
        doc_bin.to_disk(args.training_save_path)
        print(f":: Saved training data {args.training_save_path}")

        doc_bin = DocBin(docs=test_docs)
        doc_bin.to_disk(args.testing_save_path)
        print(f":: Saved test data to {args.testing_save_path}")
    elif args.command == "extract":
        results = multi_extract(args)
        with open(args.save_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(results, indent=4))
            print(f':: Saved data to file {args.save_path}')
    elif args.command == "predict":
        results = multi_predict(args)
        with open(args.save_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(results, indent=4))
            print(f':: Saved data to file {args.save_path}')
    elif args.command == 'rename':
        try:
            if args.sub_command == 'extract':
                results = multi_extract(args)
            elif args.sub_command == 'predict':
                results = multi_predict(args)
            elif args.sub_command == 'from':
                with open(args.load_from_file, 'r', encoding='utf-8') as f:
                    results = json.load(f)

            renamed, skipped, failed = rename_files(results)
            results = {"renamed": renamed,
                       "skipped": skipped, "failed": failed}
            print(
                f":: Out of total of {len(renamed) + len(skipped) + len(failed)}:: renamed: {len(renamed)}, skipped: {len(skipped)}, failed: {len(failed)}")
            with open(args.save_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(results, indent=4))
                print(f':: Saved restore data to file {args.save_path}')
        except (FileNotFoundError, PermissionError, IOError) as e:
            print(f'Error opening file ({args.load_from_file}): {e}')


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
