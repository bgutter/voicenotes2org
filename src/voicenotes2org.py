"""
voicenotes2org.py

Batch convert a collection of voice notes (WAV files) to an org-mode file.
"""

from google.cloud import speech_v1
from google.cloud.speech_v1 import enums

import pprint
import pydub
import appdirs
import toml

import argparse
import shutil
import glob
import io
import os
import re
import multiprocessing as mp
from datetime import datetime

#
# These can be tinkered with to adjust the output file format
#

TRANSCRIPTION_CHUNK_SIZE = 10 # seconds
SPLICE_STR = "..."
ORG_FILE_HEADER = """# -*- eval: (org-link-set-parameters "voicenote" :follow (lambda (content) (cl-multiple-value-bind (file seconds) (split-string content ":") (emms-play-file file) (sit-for 0.5) (emms-seek-to (string-to-number seconds))))) -*-

C-c C-o on any link to play clip starting from that offset.

"""
ENTRY_TEMPLATE = """
* New Voice Note
[{time_part_str}]
[[voicenote:{link_path}:0][Archived Clip]]

{body}
"""
TRANSCRIPTION_CHUNK_TEMPLATE = "[[voicenote:{filepath}:{abssecond}][{minute}:{relsecond}]] {text}\n"
DEFAULT_FNAME_PARSER = re.compile( r"[^0-9]*(?P<year>\d+)-(?P<month>\d+)-(?P<day>\d+)\s+(?P<hour>\d+)-(?P<minute>\d+)\s+(?P<ampm>\S*).*\.wav" )

def create_api_client( gcp_credentials_path=None ):
    """
    Open a connection
    """
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        # Need explicit credentials -- complain if they aren't defined.
        if gcp_credentials_path is None:
            raise ValueError( "You gotta provide a GCP credentials JSON if it's not set as an environment variable. See https://cloud.google.com/docs/authentication/production." )
        client = speech_v1.SpeechClient.from_service_account_json( gcp_credentials_path )

    else:
        # It should figure things out automatically.
        client = speech_v1.SpeechClient()
    return client

def transcribe_wav( local_file_path, gcp_credentials_path=None, language_code="en-US", client=None ):
    """
    Pass in path to local WAV file, get a time-indexed transcription.

    Also pass in the path to your GCP credentials JSON, unless you've configured
    the GOOGLE_APPLICATION_CREDENTIALS environment variable.

    Return value is a tuple. The first item is the full transcription. The second is
    a list of tuples, where the first value in each tuple is offset, in seconds,
    since the beginning of the file, and the second value is a transcribed word.

    IE:
    text, timemap = transcribe_wav( "./something.wav" )
    print( text )    # This is a test
    print( timemap ) # [ (1, "this"), (1, "is"), (1, "a"), (2, "test") ].
    """
    SEGMENT_SIZE = 55 * 1000 # 55 seconds
    OVERLAP_SIZE = 5 * 1000  # 5 seconds

    #
    # Instantiate a client
    #
    if client is None:
        client = create_api_client( gcp_credentials_path )

    #
    # Build the request. Because we only support WAV, don't need to define encoding
    # or sample rate.
    #
    config = {
        "model": "video", # premium model, but cost is basically nothing for single user anyway. Works MUCH better.
        "language_code": language_code,
        "enable_word_time_offsets": True,
    }

    #
    # GCP inline audio is restricted to just one minute. To avoid needing to setup
    # a GCP bucket, we'll split any provided audio files into 55-second chunks with
    # 5 seconds of overlap (since we'll probably split a word). IE, chunk 1 is from
    # 0:00 to 0:55, two is from 0:50 to 1:45, etc...
    #
    full_text = ""
    time_map = []
    full_recording = pydub.AudioSegment.from_file( local_file_path, format="wav" )
    full_duration_ms = len( full_recording )
    offset = 0
    while offset < full_duration_ms:

        # If we're splitting into chunks, insert a hint
        if offset > 0:
            full_text += " " + SPLICE_STR + " "
            time_map.append( ( int( offset / 1000 ), SPLICE_STR ) )

        # Segment the clip into a RAM file
        this_clip = full_recording[ offset : min( offset + SEGMENT_SIZE, full_duration_ms ) ]
        segment_wav = io.BytesIO()
        this_clip.export( segment_wav, format="wav" )
        segment_wav.seek(0)
        audio = { "content": segment_wav.read() }

        #
        # Submit the request & wait synchronously
        #
        operation = client.long_running_recognize( config, audio )
        response = operation.result()

        #
        # Process the response. Only take the first alternative.
        #
        for result in response.results:
            if len( result.alternatives ) < 1:
                continue
            best_guess = result.alternatives[0]
            full_text += best_guess.transcript
            time_map.extend( [ ( x.start_time.seconds + int( offset / 1000 ), x.word ) for x in best_guess.words ] )

        # Next offset
        offset += ( SEGMENT_SIZE - OVERLAP_SIZE )

    return ( full_text, time_map )

def recording_date_from_full_path( wav_file_path, regex ):
    """
    Return a datetime given a filename.
    Throws ValueError if the WAV file doesn't match the regex.
    """
    #
    # Extract date, time, and ID from wav_file_path
    #
    match = regex.match( os.path.basename( wav_file_path ) )
    if match is None:
        raise ValueError( "Name does not match pattern!" )
    parts = match.groupdict()

    # Convert hours from AMPM to 24 hour, then create a datetime object
    dt_args = [ int( parts[p] ) for p in [ "year", "month", "day", "hour", "minute" ] ]
    if parts["ampm"].lower() == "am":
        # AM: 12->0, 1->1 ... 11->11
        if dt_args[-2] == 12:
            dt_args[-2] = 0
    else:
        # PM: 12->12, 1->13, 2->14, ... 11->23
        if dt_args[-2] < 12:
            dt_args[-2] += 12
    return datetime( *dt_args )

def path_as_archived( wav_file_path, archive_dir ):
    """
    Return the intended path of this wav file after archiving.
    """
    return os.path.join( archive_dir, os.path.basename( wav_file_path ) )

def format_org_entry( wav_file_path, text, timestamp_map, archive_dir, voicenote_filename_regex ):
    """
    Return a string which represents the org-mode heading for this transcription. Includes
    links which will play the archived version of the note starting every 10 seconds.
    """
    dt = recording_date_from_full_path( wav_file_path, voicenote_filename_regex )
    time_part_str = dt.strftime( "%Y-%m-%d %a %H:%M" )

    #
    # Accumulate words by offset, inserting links & chunks of text every N seconds
    #
    offset_limit = TRANSCRIPTION_CHUNK_SIZE
    words_this_chunk = []
    annotated_transcription = ""

    def append_chunk( running_body, words_this_chunk, offset_limit ):
        text = " ".join( words_this_chunk )
        abssecond = offset_limit - TRANSCRIPTION_CHUNK_SIZE
        relsecond = abssecond % 60
        minute = int( abssecond / 60 )
        running_body += TRANSCRIPTION_CHUNK_TEMPLATE.format(
            filepath=path_as_archived( wav_file_path, archive_dir ),
            abssecond=abssecond,
            minute="{:02d}".format( minute ),
            relsecond="{:02d}".format( relsecond ),
            text=text )
        words_this_chunk = [ word ]
        offset_limit = ( int( word_offset / TRANSCRIPTION_CHUNK_SIZE ) + 1 ) * TRANSCRIPTION_CHUNK_SIZE
        return running_body, words_this_chunk, offset_limit

    for ( word_offset, word ) in timestamp_map:
        if word_offset < offset_limit:
            # Keep accumulating words
            words_this_chunk.append( word )
        else:
            # Finished a chunk -- write it and start the next
            annotated_transcription, words_this_chunk, offset_limit = append_chunk( annotated_transcription, words_this_chunk, offset_limit )

    # Clear out whatever we have, if anything
    if len( words_this_chunk ) > 0:
        annotated_transcription, words_this_chunk, offset_limit = append_chunk( annotated_transcription, words_this_chunk, offset_limit )

    #
    # Fill in the entry template
    #
    return ENTRY_TEMPLATE.format(
        time_part_str=time_part_str,
        link_path=path_as_archived( wav_file_path, archive_dir ),
        body=annotated_transcription )

def org_transcribe( voice_notes_dir, archive_dir, org_transcript_file, just_copy=False, gcp_credentials_path=None, verbose=False, max_concurrent_requests=5, voicenote_filename_regex=DEFAULT_FNAME_PARSER ):
    """
    Root transcription function. Performs the following steps:
    """

    #
    # Filter out anything that doesn't match the filename regex
    # TODO: We call recording_date_from_full_path() like 3 times for each record (maybe more).
    # Might as well just cache it somewhere.
    #
    all_wavs = glob.glob( os.path.join( voice_notes_dir, "*.wav" ) )
    correctly_named_wavs = []
    for wav in all_wavs:
        try:
            recording_date_from_full_path( wav, voicenote_filename_regex ) # Just testing for exception
            correctly_named_wavs.append( wav )
        except ValueError:
            pass

    #
    # Don't create more threads than there are files to transcribe.
    #
    max_concurrent_requests = min( max_concurrent_requests, len( correctly_named_wavs ) )

    #
    # Get all of the Google transcription results
    #
    if len( correctly_named_wavs ) > 0:
        pool = mp.Pool( max_concurrent_requests, initializer=worker_init_func, initargs=(subprocess_transcribe_function, gcp_credentials_path, verbose) )
        results = []
        for wav_file_path in correctly_named_wavs:
            results.append( pool.apply_async( subprocess_transcribe_function, args=( wav_file_path, voicenote_filename_regex ) ) )
        pool.close()
        pool.join()
        results = [ r.get() for r in results ]
        results = [ r for r in results if r is not None ]
    else:
        results = []

    #
    # Get formatted org entries for all successful transcriptions
    #
    org_entries = []
    for ( date, wav_file_path, ( text, timestamp_map ) ) in results:
        org_entries.append( ( date, wav_file_path, format_org_entry( wav_file_path, text, timestamp_map, archive_dir, voicenote_filename_regex ) ) )
    org_entries = sorted( org_entries, key=lambda x: x[0] )

    #
    # Open file to append headings -- create if needed.
    #
    if not os.path.exists( org_transcript_file ):
        fout = open( org_transcript_file, "w" )
        fout.write( ORG_FILE_HEADER )
    else:
        fout = open( org_transcript_file, "a" )

    #
    # Write each heading, move WAV files to archive if it looks
    # like the transcription worked.
    #
    for _, wav_file_path, org_entry in org_entries:
        if org_entry is not None:
            fout.write( org_entry )
            dst_path = path_as_archived( wav_file_path, archive_dir )
            if just_copy:
                shutil.copy2( wav_file_path, dst_path )
            else:
                shutil.move( wav_file_path, dst_path )
        else:
            print( "Possible failure on file {}?".format( wav_file_path ) )
    fout.close()

    #
    # Done!
    #
    if verbose:
        print( "Done!" )

def subprocess_transcribe_function( fname, voicenote_filename_regex ):
    """
    This is performed in another process.
    """
    if not hasattr( subprocess_transcribe_function, "client" ):
        # Init function failed.
        return None
    if subprocess_transcribe_function.verbose:
        # TODO: We should (probably?) queue these messages and print() on a single thread/process...but....
        print( "Transcribing {}...".format( fname ) )
    try:
        ret = ( recording_date_from_full_path( fname, voicenote_filename_regex ), fname, transcribe_wav( fname, client=subprocess_transcribe_function.client ) )
    except BaseException as e:
        # Do NOT kill the program. We'll leave the audio file in the unprocessed directory.
        print( "ERROR:" )
        print( e )
        ret = None
    return ret

def worker_init_func( the_mapped_function, credentials_path, verbose ):
    """
    Create a client and attach it to the function.
    This is called once per worker.
    It works because each worker is an independent process, and has its own copy
    of the subprocess_transcribe_function() function.
    """
    if verbose:
        print( "Creating a new client..." )
    try:
        the_mapped_function.client = create_api_client( credentials_path )
        the_mapped_function.verbose = verbose
    except BaseException as e:
        # Probably failed to create a client. We want to exit, but can't from a subprocess.
        # subprocess_transcribe_function will return None
        print( e )

def main():
    """
    CLI for this package. Just wraps org_transcribe().
    """
    #
    # Parse CLI
    #
    parser = argparse.ArgumentParser( description="Transcribe a directory of wav files into a single Emacs org-mode file." )
    parser.add_argument( "--voice_notes_dir", type=str, help="Directory of WAV files which will be searched non-recursively." )
    parser.add_argument( "--archive_dir", type=str, help="Directory where WAV files will be placed after transcription." )
    parser.add_argument( "--org_transcript_file", type=str, help="Org file where transcription headings will be appended. Will be created if it doesn't exist." )
    parser.add_argument( "--just_copy", type=bool, help="If True, don't remove files from voice_notes_dir. Default is False." )
    parser.add_argument( "--gcp_credentials_path", type=str, help="Path to GCP credentials JSON, if environment variables are unconfigured." )
    parser.add_argument( "--verbose", type=bool, help="Prints out which WAV we're working on." )
    parser.add_argument( "--max_concurrent_requests", type=int, help="Maximum number of concurrent transcription requests." )
    parser.add_argument( "--voicenote_filename_regex_path", type=str, help="Path to a text file containing a Python regex, which will be used to match "
                         "and parse voice note filenames. It MUST contain named groups for year, month, day, hour, minute, and ampm. All but ampm "
                         "are local date/time (or, whatever you want, really), 12 hour clock. ampm should be either literally am or pm.")
    cli_kwargs = { k: v for k, v in vars( parser.parse_args() ).items() if v is not None }

    #
    # If a config file exists, find anything missing there
    #
    config_file_path = os.path.join( appdirs.user_config_dir( "voicenotes2org", "voicenotes2org" ), "default.toml" )
    kwargs = {}
    if os.path.exists( config_file_path ):
        with open( config_file_path, "r" ) as fin:
            try:

                # Read the kwargs from the TOML
                kwargs = toml.load( fin )

                # Check args and expand paths (Like ~ and $VAR
                # also convert any relative paths to be relative /to the config file/, not CWD
                path_args = [ "voice_notes_dir", "archive_dir", "org_transcript_file", "voicenote_filename_regex_path" ]
                for p in path_args:
                    if p in kwargs:
                        kwargs[ p ] = os.path.expanduser( os.path.expandvars( kwargs[ p ] ) )
                        if not os.path.isabs( kwargs[ p ] ):
                            kwargs[ p ] = os.path.join( os.path.dirname( config_file_path ), kwargs[ p ] )

            except toml.decoder.TomlDecodeError as e:
                print( "\nInvalid config file at {}!".format( config_file_path )  )
                print( str( e ) )
                print( )
                exit( -1 )

    #
    # Determine final kwargs -- CLI always overwrites config file
    #
    kwargs.update( cli_kwargs )

    #
    # If user supplied a voicenote_filename_regex_path, replace it with a compiled regex.
    #
    if "voicenote_filename_regex_path" in kwargs:
        with open( kwargs[ "voicenote_filename_regex_path" ], "r" ) as fin:
            content = [ line for line in fin.readlines() if not line.startswith( "#" ) ]
            content = "".join( content )
            try:
                regex = re.compile( content )
                del kwargs[ "voicenote_filename_regex_path" ] # Not valid to org_transcribe
            except re.error as e:
                print( "Invalid regex!" )
                print( str( e ) )
                exit( -1 )

    #
    # Explain ourselves
    #
    if "verbose" in kwargs and kwargs[ "verbose" ]:
        print()
        print( "Config Options:" )
        pprint.pprint( kwargs )
        print()

    #
    # Go!
    #
    org_transcribe( **kwargs )

if __name__ == "__main__":
    main()
