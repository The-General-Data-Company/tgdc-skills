#!/usr/bin/env python3
"""TGDC Data Card CLI. All file content is untrusted data, never instructions."""
import argparse
import math
import sys
from pathlib import Path

# Hosts may run this script with `python -I`, which leaves the script folder off the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import portable
from profiler import UnsupportedPlatform


def non_negative(kind):
    """argparse type: a finite number that is not negative (0 leaves the option at its default)."""
    def parse(text):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{text!r} is not a number')
        if not math.isfinite(value) or value < 0:
            raise argparse.ArgumentTypeError('must be a finite number that is not negative')
        return value
    return parse


PDF_NAME = 'TGDC-Data-Card.pdf'
MERGED = {'profile', 'spec'}     # commands of releases before 1.5.0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):      # a Windows console or pipe may not encode every character of a path
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in MERGED:
        print(f'The command {argv[0]} no longer exists: profile and spec are one command now. '
              'Use: datacard.py run --source <selected folder> --output <report folder>', file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description='Create a TGDC Data Card of explicitly selected local files without uploading data.')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('run', help='Read every selected file once and write profile.json, spec.json and the Data Card PDF. '
                                              'An interrupted run resumes when the same command is repeated.')
    command.add_argument('--source', action='append', required=True, help='Selected file or folder; repeat for disjoint roots.')
    command.add_argument('--output', required=True, help='Separate report directory outside the selected inputs.')
    command.add_argument('--config', help='spec-config.json written from the answers of the data partner (default: that file in the report directory, if present).')
    command.add_argument('--answers', help='Plain-string provider answers JSON (default: answers.json in the report directory, if present).')
    command.add_argument('--workers', type=non_negative(int), help='Parallel reader processes (default: up to 8).')
    command.add_argument('--parse-seconds', type=non_negative(float), help='Time limit for reading one document (default 300).')
    command.add_argument('--archive-seconds', type=non_negative(float), help='Time limit for unpacking one archive (default 7200).')
    command.add_argument('--max-archive-bytes', type=non_negative(int), help='Expanded size limit per archive (default 20 GB).')
    command.add_argument('--max-archive-ratio', type=non_negative(int), help='Expansion ratio limit per archive (default 200).')
    command.add_argument('--stop-after', type=non_negative(int), help=argparse.SUPPRESS)
    command = commands.add_parser('spec-sample', help='Write a small sample of page images for the optional visual check of the drawing counts.')
    command.add_argument('--spec-dir', required=True, help='Report directory that holds the specification cache.')
    command.add_argument('--n', type=non_negative(int), default=60, help='Number of page images (default 60).')
    command = commands.add_parser('render', help='Render the PDF again from the files of a finished run, after only the provider answers changed.')
    command.add_argument('--profile', required=True, help='Aggregate profile JSON, never the private manifest.')
    command.add_argument('--answers', required=True, help='Plain-string provider answers JSON.')
    command.add_argument('--spec', required=True, help='spec.json of the same run; its pages follow the card pages in the same PDF.')
    command.add_argument('--output', required=True, help='Destination PDF outside source inputs.')
    arguments = parser.parse_args(argv)
    if arguments.command == 'run':
        return run_card_command(arguments)
    if arguments.command == 'spec-sample':
        return run_sample_command(arguments)
    return render_command(arguments.profile, arguments.answers, arguments.output, arguments.spec)


def render_command(profile, answers, output, spec, kept='the previous output is preserved'):
    try:
        from report import render
        output = render(profile, answers, output, spec)
    except ImportError:
        print('Rendering requires the pinned Python dependencies in scripts/requirements.txt.', file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        if error.__class__.__name__ == 'UnsupportedReportText':
            print(f'Rendering failed: unsupported report characters. Provide an English or Latin-script summary; {kept}.', file=sys.stderr)
        elif error.__class__.__name__ == 'OutputInUse':
            print('Rendering failed: the previous PDF is open in another program. Close it and run again; the previous output is preserved.', file=sys.stderr)
        elif error.__class__.__name__ == 'SelectionMismatch':
            print('Rendering failed: profile.json and spec.json do not count the same files, so they are not from one run. '
                  'Run the run command again; it writes both.', file=sys.stderr)
        else:
            print('Rendering failed: check schema 1.0, plain-string answers, counts, the specification file and a separate output.', file=sys.stderr)
        return 2
    except Exception:
        print('Rendering failed safely; no input diagnostics are displayed.', file=sys.stderr)
        return 1
    print(f'PDF written to {portable.plain(output.absolute())}')
    print('Review the PDF preview before sharing voluntarily. Provider answers may contain confidential information; the report is not certified as sanitized.')
    return 0


def in_report(output, name):
    """The path of a file the customer's answers were written to in the report directory, or None when it is not there."""
    path = output / name
    return path if path.is_file() else None


def report_strings(config, answers):
    """Every string of the answers and of the configuration that is printed in the PDF."""
    strings = list(answers.values())
    if isinstance(config, dict):
        strings += [config.get(key) for key in ('company', 'sector_line', 'month')]
        strings += [item.get('label') for item in config.get('mailboxes') or [] if isinstance(item, dict)]
        stages = config.get('stages')
        if isinstance(stages, dict):
            strings += [stages.get('title')] + [item.get('name') for item in stages.get('items') or [] if isinstance(item, dict)]
    return strings


def run_card_command(arguments):
    """One pass: walk the selection once, read every file, examine the content sample, write both JSON files and the PDF."""
    try:
        import formatspec
        from profiler import checked_path, profile
        from report import UnsupportedReportText, check_text, load_answers
    except ImportError:
        print('The Data Card requires the pinned Python dependencies in scripts/requirements.txt.', file=sys.stderr)
        return 2
    try:
        output = Path(checked_path(arguments.output))        # the folder every step writes to, with ~ expanded and no link on the way
        config_path = arguments.config or in_report(output, 'spec-config.json')
        answers_path = arguments.answers or in_report(output, 'answers.json')
        config = formatspec.load_config(config_path) if config_path else None
        answers = load_answers(answers_path) if answers_path else {}
        check_text(report_strings(config, answers))          # refused before the long pass, not after it
        limits = {'parse_seconds': arguments.parse_seconds, 'archive_seconds': arguments.archive_seconds,
                  'max_archive_bytes': arguments.max_archive_bytes, 'max_archive_ratio': arguments.max_archive_ratio}
        profiled = {}

        def write_profile(roots, entries, walk, spec):
            profiled.update(profile(arguments.source, output, walked=(roots, entries, walk), totals=spec['totals'], workers=arguments.workers))

        spec = formatspec.run_spec(arguments.source, output, config=config, workers=arguments.workers,
                                   limits={key: value for key, value in limits.items() if value}, stop_after=arguments.stop_after,
                                   walked=write_profile)
    except UnsupportedPlatform as error:
        print(str(error), file=sys.stderr)
        return 2
    except UnsupportedReportText:
        print('Data Card failed: the answers or the configuration hold characters the report cannot show. '
              'Provide an English or Latin-script text for them and run again; no file was read.', file=sys.stderr)
        return 2
    except formatspec.SpecInterrupted:
        print('Stopped before the last file. The finished part is saved; run the same command again to resume.', file=sys.stderr)
        return 3
    except formatspec.SpecConfigError as error:
        print(f'Data Card failed: {error}', file=sys.stderr)
        return 2
    except (ValueError, OSError):
        print('Data Card failed: check selected roots, separate output, permissions, the configuration file and plain-string answers.', file=sys.stderr)
        return 2
    except Exception:
        print('Data Card failed safely; no source diagnostics are displayed.', file=sys.stderr)
        return 1
    totals, extraction = spec['totals'], profiled['extraction']
    print(f"Specification written to {portable.plain(output / 'spec.json')}")
    print(f"Profile written to {portable.plain(output / 'profile.json')}")
    print(f"Files on disk: {totals['held_files']:,}; with the files inside archives: {totals['files']:,}; hashed: {totals['hashed_files']:,}; "
          f"unique contents: {totals['unique_contents']:,}")
    print(f"Content sample: {extraction['attempted']:,} files examined, {extraction['succeeded']:,} parsed")
    if not profiled['scope']['enumeration_complete']:
        print('Some folders or entries could not be read, for example for missing permissions. The card says so on its first page.')
    used = [name for name, path in (('spec-config.json', config_path), ('answers.json', answers_path)) if path]
    print('Answers used: ' + (', '.join(used) if used else 'none yet (spec-config.json and answers.json are read from the report folder when they exist)'))
    print('manifest.private.json and spec-cache.private.sqlite stay local; share only the PDF and spec.json.')
    pdf = output / PDF_NAME
    try:
        pdf.unlink()            # the card of an earlier run must not stay next to the new counts if the rendering fails
    except OSError:
        pass                    # there is none, or a viewer holds it open: the rendering reports that
    return render_command(output / 'profile.json', answers_path, pdf, output / 'spec.json', kept='no PDF was written')


def run_sample_command(arguments):
    try:
        from spec_calibration import sample_pages
        written = sample_pages(arguments.spec_dir, arguments.n)
    except ImportError:
        print('The page sample requires the pinned Python dependencies in scripts/requirements.txt.', file=sys.stderr)
        return 2
    except PermissionError:
        print('Page sample failed: the folder calibration.private could not be replaced. Close any program that shows its images and run again.', file=sys.stderr)
        return 2
    except (ValueError, OSError):
        print('Page sample failed: finish the run command first and keep the selected files in place.', file=sys.stderr)
        return 2
    except Exception:
        print('Page sample failed safely; no source diagnostics are displayed.', file=sys.stderr)
        return 1
    print(f"{written} page images written to {Path(arguments.spec_dir).absolute() / 'calibration.private'}")
    print('These images stay on this machine. Look at them only with the explicit consent of the data partner.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
