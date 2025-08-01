import os
import subprocess
import warnings
import time
import tempfile
import re
import logging
import uuid
import sys
import shutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, CallbackQueryHandler, CallbackContext, Filters
from telegram.error import BadRequest, TelegramError, TimedOut, NetworkError

# השתקת אזהרות
warnings.filterwarnings("ignore")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# הגדרת לוגים
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING,  # שינוי לרמת WARNING כדי להפחית רעש בלוגים
    handlers=[
        logging.FileHandler('bot_errors.log', encoding='utf-8'),  # הוספת encoding='utf-8' לתמיכה בעברית
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ייבוא מודולים נדרשים
try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    logger.error("אנא התקן את הספרייה openai-whisper")
    WHISPER_AVAILABLE = False

try:
    from googletrans import Translator
    TRANSLATOR_AVAILABLE = True
except ImportError:
    logger.error("אנא התקן את הספרייה googletrans==3.1.0a0")
    TRANSLATOR_AVAILABLE = False

try:
    import ffmpeg
    FFMPEG_AVAILABLE = True
except ImportError:
    logger.error("אנא התקן את הספרייה ffmpeg-python")
    FFMPEG_AVAILABLE = False

# מילון צבעים
COLOR_CODES = {
    "white": "&H00FFFFFF&",
    "yellow": "&H0000FFFF&",
    "black": "&H00000000&",
    "tomato": "&H000066FF&"
}

# הגדרות
BASE_TEMP_DIR = tempfile.gettempdir()
# יצירת תיקייה פשוטה למניעת בעיות נתיבים
SIMPLE_TEMP_DIR = os.path.join(os.path.expanduser("~"), "tempfiles")
if not os.path.exists(SIMPLE_TEMP_DIR):
    try:
        os.makedirs(SIMPLE_TEMP_DIR)
    except:
        SIMPLE_TEMP_DIR = BASE_TEMP_DIR  # נופל בחזרה לתיקיית ברירת המחדל

MAX_FILE_SIZE_MB = 200
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 240

def check_dependencies():
    """בדיקת כל התלויות הנדרשות"""
    missing = []
    
    if not WHISPER_AVAILABLE:
        missing.append("openai-whisper")
    if not TRANSLATOR_AVAILABLE:
        missing.append("googletrans==3.1.0a0")
    if not FFMPEG_AVAILABLE:
        missing.append("ffmpeg-python")
    
    # בדיקת FFmpeg
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, 
                              check=True, 
                              timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        missing.append("FFmpeg")
    
    return missing

def safe_file_operation(func, *args, **kwargs):
    """ביצוע פעולת קובץ בצורה בטוחה"""
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except (PermissionError, OSError) as e:
            if attempt < max_attempts - 1:
                time.sleep(0.5)
                continue
            logger.error(f"שגיאה בפעולת קובץ: {e}")
            return None
        except Exception as e:
            logger.error(f"שגיאה כללית בפעולת קובץ: {e}")
            return None
    return None

def cleanup_files(*file_paths):
    """ניקוי קבצים בצורה בטוחה"""
    for file_path in file_paths:
        if file_path and os.path.exists(file_path):
            try:
                safe_file_operation(os.remove, file_path)
            except Exception as e:
                logger.warning(f"לא ניתן למחוק קובץ {file_path}: {e}")

def get_file_info_safe(update):
    """קבלת מידע על הקובץ בצורה בטוחה"""
    try:
        if update.message.video:
            video = update.message.video
            return {
                'file_id': video.file_id,
                'file_size': getattr(video, 'file_size', 0),
                'file_type': 'video',
                'file_name': getattr(video, 'file_name', 'video.mp4'),
                'duration': getattr(video, 'duration', 0)
            }
        elif (update.message.document and 
              hasattr(update.message.document, 'mime_type') and
              update.message.document.mime_type and 
              update.message.document.mime_type.startswith('video/')):
            document = update.message.document
            return {
                'file_id': document.file_id,
                'file_size': getattr(document, 'file_size', 0),
                'file_type': 'document',
                'file_name': getattr(document, 'file_name', 'video.mp4'),
                'duration': 0
            }
    except Exception as e:
        logger.error(f"שגיאה בקבלת מידע על הקובץ: {e}")
    
    return None

def download_file_safe(context, file_id, file_path, max_retries=2):
    """הורדת קובץ בצורה בטוחה עם טיפול בכל השגיאות האפשריות"""
    
    for attempt in range(max_retries):
        try:
            logger.info(f"מתחיל הורדה - ניסיון {attempt + 1}")
            
            # נסה לקבל את המידע על הקובץ
            file_obj = context.bot.get_file(file_id, timeout=60)
            
            # בדיקת גודל הקובץ מול המגבלה של הבוט
            if hasattr(file_obj, 'file_size') and file_obj.file_size:
                if file_obj.file_size > 20 * 1024 * 1024:
                    logger.error(f"הקובץ גדול מדי: {file_obj.file_size / (1024*1024):.1f}MB")
                    return False, "הקובץ גדול מדי עבור API של טלגרם (מקסימום 20MB)"
            
            # נסה להוריד
            file_obj.download(file_path, timeout=DOWNLOAD_TIMEOUT)
            
            # וודא שהקובץ הורד בהצלחה
            if os.path.exists(file_path) and os.path.getsize(file_path) > 100:  # לפחות 100 בייט
                logger.info(f"הקובץ הורד בהצלחה: {file_path}")
                return True, "הורד בהצלחה"
                
        except BadRequest as e:
            error_msg = str(e).lower()
            if "file is too big" in error_msg:
                return False, "הקובץ גדול מדי עבור API של טלגרם"
            elif "file not found" in error_msg:
                return False, "הקובץ לא נמצא או פג תוקפו"
            elif "invalid file id" in error_msg:
                return False, "מזהה קובץ לא תקין"
            else:
                logger.error(f"שגיאת BadRequest: {e}")
                
        except (TimedOut, NetworkError) as e:
            logger.warning(f"שגיאת רשת בניסיון {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # המתנה מתגברת
                continue
                
        except TelegramError as e:
            logger.error(f"שגיאת טלגרם: {e}")
            
        except Exception as e:
            logger.error(f"שגיאה כללית בהורדה: {e}")
            
    return False, "נכשל להוריד את הקובץ לאחר מספר נסיונות"

def extract_audio_safe(video_path, user_id):
    """מיצוי אודיו בצורה בטוחה"""
    audio_path = os.path.join(SIMPLE_TEMP_DIR, f"audio_{user_id}_{int(time.time())}.wav")
    
    try:
        # מחיקת קובץ אודיו קיים
        cleanup_files(audio_path)
        
        # מיצוי האודיו - שינוי בהגדרות ffmpeg כדי להסיר את capture_output שלא נתמך
        (
            ffmpeg
            .input(video_path)
            .output(audio_path, acodec='pcm_s16le', ac=1, ar='16000')
            .overwrite_output()
            .run(quiet=True)
        )
        
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            return audio_path
        else:
            raise Exception("לא ניתן לחלץ אודיו מהסרטון")
            
    except Exception as e:
        cleanup_files(audio_path)
        raise Exception(f"שגיאה במיצוי אודיו: {str(e)}")

def translate_text_safe(text, max_retries=3):
    """תרגום טקסט בצורה בטוחה"""
    if not text or not text.strip():
        return text
        
    translator = Translator()
    
    for attempt in range(max_retries):
        try:
            result = translator.translate(text.strip(), src='en', dest='iw')
            if result and result.text:
                return result.text
        except Exception as e:
            logger.warning(f"שגיאה בתרגום (ניסיון {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            
    return text  # החזר טקסט מקורי אם התרגום נכשל

def add_space_after_punctuation(text):
    """הוספת רווחים אחרי סימני פיסוק"""
    if not text:
        return text
        
    punctuation_marks = [',', '.', '!', '?', ':', ';', ')', ']', '}']
    
    for mark in punctuation_marks:
        text = re.sub(f'\\{mark}(?!\\s)', f'{mark} ', text)
    
    return text

def process_segments_safe(segments, status_message, context):
    """עיבוד ותרגום סגמנטים בצורה בטוחה"""
    hebrew_segments = []
    total_segments = len(segments)
    
    for i, segment in enumerate(segments):
        try:
            # עדכון סטטוס כל 5 סגמנטים
            if i % 5 == 0 or i == total_segments - 1:
                try:
                    context.bot.edit_message_text(
                        chat_id=status_message.chat_id,
                        message_id=status_message.message_id,
                        text=f"🌐 מתרגם...\n"
                        f"קטע {i+1} מתוך {total_segments}\n"
                        f"({int((i+1)/total_segments*100)}%)"
                    )
                except:
                    pass  # התעלם משגיאות עדכון סטטוס
            
            # קבלת הטקסט והוספת רווחים
            original_text = segment.get('text', '').strip()
            if not original_text:
                continue
                
            # תרגום
            translated_text = translate_text_safe(original_text)
            translated_text = add_space_after_punctuation(translated_text)
            
            hebrew_segments.append({
                'start': segment.get('start', 0),
                'end': segment.get('end', 0),
                'text': translated_text
            })
            
        except Exception as e:
            logger.warning(f"שגיאה בעיבוד סגמנט {i}: {e}")
            # הוסף את הטקסט המקורי במקרה של שגיאה
            hebrew_segments.append({
                'start': segment.get('start', 0),
                'end': segment.get('end', 0),
                'text': segment.get('text', '')
            })
    
    return hebrew_segments

def format_srt_time(seconds):
    """המרת שניות לפורמט SRT"""
    try:
        seconds = float(seconds)
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        millisecs = int((secs % 1) * 1000)
        secs = int(secs)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"
    except:
        return "00:00:00,000"

def create_srt_file_safe(segments, user_id):
    """יצירת קובץ SRT בצורה בטוחה"""
    srt_path = os.path.join(SIMPLE_TEMP_DIR, f"subs_{user_id}_{int(time.time())}.srt")
    
    try:
        with open(srt_path, 'w', encoding='utf-8') as f:
            subtitle_index = 1
            
            for segment in segments:
                if not segment.get('text'):
                    continue
                    
                start_time = format_srt_time(segment.get('start', 0))
                end_time = format_srt_time(segment.get('end', 0))
                
                f.write(f"{subtitle_index}\n")
                f.write(f"{start_time} --> {end_time}\n")
                f.write(f"{segment['text']}\n\n")
                
                subtitle_index += 1
        
        return srt_path
        
    except Exception as e:
        cleanup_files(srt_path)
        raise Exception(f"שגיאה ביצירת קובץ כתוביות: {str(e)}")

def embed_subtitles_safe(video_path, srt_path, user_id, font_size, font_color):
    """הטמעת כתוביות בצורה בטוחה - תיקון מלא לנתיבים בחלונות"""
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    timestamp = int(time.time())
    
    # יצירת שמות קבצים פשוטים
    work_dir = SIMPLE_TEMP_DIR
    simple_video_path = os.path.join(work_dir, f"v_{user_id}_{timestamp}.mp4")
    simple_srt_path = os.path.join(work_dir, f"s_{user_id}_{timestamp}.srt")
    output_path = os.path.join(work_dir, f"o_{user_id}_{timestamp}.mp4")
    
    try:
        # העתקת הקבצים לשמות פשוטים
        shutil.copyfile(video_path, simple_video_path)
        shutil.copyfile(srt_path, simple_srt_path)
        
        color_code = COLOR_CODES.get(font_color, "&H00FFFFFF&")
        
        # מעבר לתיקיית העבודה
        original_dir = os.getcwd()
        os.chdir(work_dir)
        
        # פקודת FFmpeg עם נתיבים יחסיים פשוטים
        simple_video_name = os.path.basename(simple_video_path)
        simple_srt_name = os.path.basename(simple_srt_path)
        output_name = os.path.basename(output_path)
        
        cmd = [
            'ffmpeg',
            '-i', simple_video_name,
            '-vf', f"subtitles={simple_srt_name}:force_style='Fontsize={font_size},PrimaryColour={color_code},OutlineColour=&H00000000&,Bold=1'",
            '-c:a', 'copy',
            '-y', output_name
        ]
        
        # הפעלת הפקודה
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900
        )
        
        # חזרה לתיקייה המקורית
        os.chdir(original_dir)
        
        # בדיקת התוצאה
        if result.returncode != 0:
            raise Exception(f"FFmpeg נכשל: {result.stderr}")
            
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
        else:
            raise Exception("קובץ הפלט לא נוצר כראוי")
            
    except subprocess.TimeoutExpired:
        os.chdir(original_dir)  # וידוא חזרה לתיקייה המקורית
        cleanup_files(simple_video_path, simple_srt_path, output_path)
        raise Exception("התהליך ארך יותר מדי זמן")
    except Exception as e:
        os.chdir(original_dir)  # וידוא חזרה לתיקייה המקורית
        cleanup_files(simple_video_path, simple_srt_path, output_path)
        raise Exception(f"שגיאה בהטמעת כתוביות: {str(e)}")

def process_video_complete(video_path, update, context):
    """תהליך עיבוד סרטון מלא עם טיפול בשגיאות"""
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    
    font_size = context.user_data.get('font_size', 24)
    font_color = context.user_data.get('font_color', 'white')
    
    audio_path = None
    srt_path = None
    output_path = None
    
    try:
        # שלב 1: טעינת מודל
        status_message = context.bot.send_message(
            chat_id=chat_id, 
            text="🧠 טוען מודל זיהוי דיבור..."
        )
        
        whisper_model = whisper.load_model("base")
        
        # שלב 2: מיצוי אודיו
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text="🎵 מחלץ אודיו מהסרטון..."
        )
        
        audio_path = extract_audio_safe(video_path, user_id)
        
        # שלב 3: זיהוי דיבור
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text="👂 מזהה דיבור באנגלית..."
        )
        
        result = whisper_model.transcribe(
            audio_path, 
            language='en', 
            word_timestamps=True,
            verbose=False
        )
        
        if not result.get('segments'):
            raise Exception("לא נמצא דיבור בסרטון")
        
        # שלב 4: תרגום
        hebrew_segments = process_segments_safe(result['segments'], status_message, context)
        
        if not hebrew_segments:
            raise Exception("לא ניתן לתרגם את הסרטון")
        
        # שלב 5: יצירת כתוביות
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text="✏️ יוצר כתוביות..."
        )
        
        srt_path = create_srt_file_safe(hebrew_segments, user_id)
        
        # שלב 6: הטמעת כתוביות
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text="🎬 מטמיע כתוביות בסרטון..."
        )
        
        output_path = embed_subtitles_safe(video_path, srt_path, user_id, font_size, font_color)
        
        # שלב 7: שליחת התוצאה
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text="📤 שולח סרטון..."
        )
        
        # בדיקת גודל קובץ הפלט
        output_size = os.path.getsize(output_path)
        if output_size > MAX_FILE_SIZE:
            raise Exception(f"קובץ הפלט גדול מדי ({output_size/(1024*1024):.1f}MB)")
        
        with open(output_path, 'rb') as video_file:
            context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption="✅ סרטון עם כתוביות בעברית!",
                supports_streaming=True,
                timeout=300
            )
        
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text="✅ התהליך הושלם בהצלחה!"
        )
        
    except Exception as e:
        error_msg = f"❌ שגיאה בעיבוד הסרטון:\n{str(e)}"
        logger.error(error_msg)
        
        try:
            context.bot.send_message(chat_id=chat_id, text=error_msg)
        except:
            pass
            
    finally:
        # ניקוי קבצים
        cleanup_files(audio_path, srt_path, output_path)

# פקודות הבוט
def start(update: Update, context: CallbackContext) -> None:
    """פקודת התחלה"""
    missing = check_dependencies()
    if missing:
        missing_str = "\n".join([f"- {pkg}" for pkg in missing])
        update.message.reply_text(
            f"⚠️ חסרים רכיבים נדרשים:\n\n{missing_str}\n\n"
            f"אנא התקן אותם כדי להשתמש בבוט."
        )
        return
    
    # אתחול הגדרות משתמש
    context.user_data.setdefault('font_size', 24)
    context.user_data.setdefault('font_color', 'white')
    
    keyboard = [[InlineKeyboardButton("⚙️ הגדרות כתוביות", callback_data='settings')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text(
        "🎬 *ברוכים הבאים למתרגם הסרטונים*\n\n"
        "שלח סרטון באנגלית ואקבל בחזרה סרטון עם כתוביות בעברית!\n\n"
        f"📁 גודל מקסימלי: {MAX_FILE_SIZE_MB}MB\n"
        "⏱️ אורך מומלץ: עד 10 דקות\n\n"
        "לחץ על הגדרות לשינוי גודל וצבע הכתוביות.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

def settings(update: Update, context: CallbackContext) -> None:
    """תפריט הגדרות"""
    try:
        query = update.callback_query
        query.answer()
        
        font_size = context.user_data.get('font_size', 24)
        font_color = context.user_data.get('font_color', 'white')
        
        color_names = {
            'white': "לבן ⚪", 'yellow': "צהוב 🟡",
            'black': "שחור ⚫", 'tomato': "אדום 🔴"
        }
        
        keyboard = [
            [InlineKeyboardButton("🔍 גודל פונט", callback_data='font_size')],
            [InlineKeyboardButton("🎨 צבע כתוביות", callback_data='font_color')],
            [InlineKeyboardButton("🏠 חזרה", callback_data='main_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        query.edit_message_text(
            f"⚙️ *הגדרות כתוביות*\n\n"
            f"🔤 גודל פונט: *{font_size}*\n"
            f"🎨 צבע: *{color_names.get(font_color, font_color)}*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except TelegramError as e:
        if "Query is too old" in str(e):
            # טיפול בשגיאת כפתור ישן
            try:
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ חלף זמן רב מדי. אנא השתמש בפקודת /settings שוב."
                )
            except:
                pass

def font_size_menu(update: Update, context: CallbackContext) -> None:
    """תפריט גודל פונט - עדכון לטווח גדלים חדש"""
    try:
        query = update.callback_query
        query.answer()
        
        font_size = context.user_data.get('font_size', 24)
        # שינוי טווח גדלי פונט ל-4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24
        sizes = [4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24]
        
        keyboard = []
        # סידור בשלוש שורות עם מספר כפתורים בכל שורה
        for i in range(0, len(sizes), 4):
            row = []
            for size in sizes[i:i+4]:
                text = f"[{size}]" if size == font_size else str(size)
                row.append(InlineKeyboardButton(text, callback_data=f'set_size_{size}'))
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton("↩️ חזרה", callback_data='settings')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        query.edit_message_text(
            f"🔤 *בחירת גודל פונט*\n\n"
            f"טווח גדלים: 4 (קטן מאוד) עד 24 (גדול)\n"
            f"נוכחי: *{font_size}*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except TelegramError as e:
        if "Query is too old" in str(e):
            try:
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ חלף זמן רב מדי. אנא השתמש בפקודת /settings שוב."
                )
            except:
                pass

def set_font_size(update: Update, context: CallbackContext) -> None:
    """הגדרת גודל פונט"""
    try:
        query = update.callback_query
        query.answer()
        
        size = int(query.data.split('_')[-1])
        context.user_data['font_size'] = size
        settings(update, context)
    except TelegramError:
        pass

def font_color_menu(update: Update, context: CallbackContext) -> None:
    """תפריט צבע פונט"""
    try:
        query = update.callback_query
        query.answer()
        
        font_color = context.user_data.get('font_color', 'white')
        color_options = [
            ("לבן ⚪", "white"), ("צהוב 🟡", "yellow"),
            ("שחור ⚫", "black"), ("אדום 🔴", "tomato")
        ]
        
        keyboard = []
        for label, color_value in color_options:
            text = f"[{label}]" if color_value == font_color else label
            keyboard.append([InlineKeyboardButton(text, callback_data=f'set_color_{color_value}')])
        
        keyboard.append([InlineKeyboardButton("↩️ חזרה", callback_data='settings')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        query.edit_message_text(
            "🎨 *בחירת צבע כתוביות*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except TelegramError:
        pass

def set_font_color(update: Update, context: CallbackContext) -> None:
    """הגדרת צבע פונט"""
    try:
        query = update.callback_query
        query.answer()
        
        color = query.data.split('_')[-1]
        context.user_data['font_color'] = color
        settings(update, context)
    except TelegramError:
        pass

def main_menu(update: Update, context: CallbackContext) -> None:
    """תפריט ראשי"""
    try:
        query = update.callback_query
        query.answer()
        
        keyboard = [[InlineKeyboardButton("⚙️ הגדרות כתוביות", callback_data='settings')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        query.edit_message_text(
            "🎬 *מתרגם הסרטונים*\n\n"
            "שלח סרטון באנגלית לתרגום!",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except TelegramError:
        pass

def handle_callback_safely(update: Update, context: CallbackContext) -> None:
    """טיפול בטוח בכל לחיצת כפתור"""
    try:
        callback_data = update.callback_query.data
        
        if callback_data == 'settings':
            settings(update, context)
        elif callback_data == 'font_size':
            font_size_menu(update, context)
        elif callback_data == 'font_color':
            font_color_menu(update, context)
        elif callback_data == 'main_menu':
            main_menu(update, context)
        elif callback_data.startswith('set_size_'):
            set_font_size(update, context)
        elif callback_data.startswith('set_color_'):
            set_font_color(update, context)
        
    except Exception as e:
        logger.warning(f"שגיאה בטיפול בכפתור: {e}")

def handle_video(update: Update, context: CallbackContext) -> None:
    """טיפול בסרטון שנשלח"""
    try:
        # בדיקת תלויות
        missing = check_dependencies()
        if missing:
            missing_str = "\n".join([f"- {pkg}" for pkg in missing])
            update.message.reply_text(f"⚠️ חסרים רכיבים:\n{missing_str}")
            return
        
        # קבלת מידע על הקובץ
        file_info = get_file_info_safe(update)
        if not file_info:
            update.message.reply_text("❌ אנא שלח קובץ וידאו תקין.")
            return
        
        # בדיקת גודל
        if file_info['file_size'] and file_info['file_size'] > MAX_FILE_SIZE:
            size_mb = file_info['file_size'] / (1024*1024)
            update.message.reply_text(
                f"❌ הקובץ גדול מדי!\n"
                f"גודל: {size_mb:.1f}MB\n"
                f"מקסימום: {MAX_FILE_SIZE_MB}MB"
            )
            return
        
        # הודעת התחלה
        processing_msg = update.message.reply_text("⏳ מוריד את הסרטון...")
        
        # הורדת הקובץ עם שם פשוט יותר
        user_id = update.effective_user.id
        timestamp = int(time.time())
        file_ext = os.path.splitext(file_info['file_name'])[-1] or ".mp4"
        video_path = os.path.join(SIMPLE_TEMP_DIR, f"input_{user_id}_{timestamp}{file_ext}")
        
        success, error_msg = download_file_safe(context, file_info['file_id'], video_path)
        
        if not success:
            try:
                context.bot.edit_message_text(
                    chat_id=processing_msg.chat_id,
                    message_id=processing_msg.message_id,
                    text=f"❌ {error_msg}"
                )
            except:
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"❌ {error_msg}"
                )
            return
        
        # הודעת הצלחה
        try:
            context.bot.edit_message_text(
                chat_id=processing_msg.chat_id,
                message_id=processing_msg.message_id,
                text="✅ הסרטון הורד! מתחיל עיבוד..."
            )
        except:
            pass
        
        # עיבוד הסרטון
        process_video_complete(video_path, update, context)
        
    except Exception as e:
        logger.error(f"שגיאה כללית בטיפול בסרטון: {e}")
        try:
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ שגיאה בלתי צפויה. אנא נסה שוב."
            )
        except:
            pass
    finally:
        # ניקוי קובץ הקלט
        try:
            if 'video_path' in locals():
                cleanup_files(video_path)
        except:
            pass

def help_command(update: Update, context: CallbackContext) -> None:
    """עזרה"""
    help_text = (
        "*📖 מדריך שימוש*\n\n"
        "*צעדים:*\n"
        "1️⃣ שלח סרטון באנגלית\n"
        "2️⃣ המתן לעיבוד\n"
        "3️⃣ קבל סרטון עם כתוביות בעברית\n\n"
        "*מגבלות:*\n"
        f"📁 גודל מקסימלי: {MAX_FILE_SIZE_MB}MB\n"
        "⏱️ אורך מומלץ: עד 10 דקות\n"
        "🎯 שפת מקור: אנגלית\n\n"
        "*פקודות:*\n"
        "/start - התחלה\n"
        "/help - עזרה זו\n"
        "/settings - הגדרות\n\n"
        "*גדלי פונט:*\n"
        "מ-4 (קטן מאוד) עד 24 (גדול)"
    )
    update.message.reply_text(help_text, parse_mode='Markdown')

def settings_command(update: Update, context: CallbackContext) -> None:
    """פקודת הגדרות"""
    context.user_data.setdefault('font_size', 24)
    context.user_data.setdefault('font_color', 'white')
    
    keyboard = [
        [InlineKeyboardButton("🔍 גודל פונט", callback_data='font_size')],
        [InlineKeyboardButton("🎨 צבע כתוביות", callback_data='font_color')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    font_size = context.user_data['font_size']
    font_color = context.user_data['font_color']
    
    color_names = {
        'white': "לבן ⚪", 'yellow': "צהוב 🟡",
        'black': "שחור ⚫", 'tomato': "אדום 🔴"
    }
    
    update.message.reply_text(
        f"⚙️ *הגדרות כתוביות*\n\n"
        f"🔤 גודל: *{font_size}*\n"
        f"🎨 צבע: *{color_names.get(font_color, font_color)}*",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

def error_handler(update: Update, context: CallbackContext) -> None:
    """טיפול בשגיאות כלליות"""
    if "Query is too old" in str(context.error):
        # התעלם משגיאות כפתור ישן - טופל בנפרד
        return
        
    logger.error(f'שגיאה: {context.error}')
    
    if update and update.effective_chat:
        try:
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ אירעה שגיאה. אנא נסה שוב."
            )
        except:
            pass

def main() -> None:
    """הפעלת הבוט"""
    TOKEN = "8179543077:AAGvg0VWYxFf0uRD4wCCU-QEw4xtCMqC8l4"
    
    # יצירת הבוט
    updater = Updater(TOKEN)
    dispatcher = updater.dispatcher

    # טיפול בשגיאות
    dispatcher.add_error_handler(error_handler)

    # הנדלרים
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("settings", settings_command))
    
    # טיפול בסרטונים
    video_filter = (Filters.video | 
                   Filters.document.mime_type("video/mp4") |
                   Filters.document.mime_type("video/avi") |
                   Filters.document.mime_type("video/mov") |
                   Filters.document.mime_type("video/quicktime"))
    
    dispatcher.add_handler(MessageHandler(video_filter, handle_video))

    # טיפול משופר בכפתורים
    dispatcher.add_handler(CallbackQueryHandler(handle_callback_safely))

    # הפעלה
    print(f"🚀 מפעיל בוט תרגום סרטונים (גרסה 3.0)")
    print(f"📁 גודל מקסימלי לקובץ: {MAX_FILE_SIZE_MB}MB")
    print(f"🔍 גדלי פונט: 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24")
    print(f"🔗 כתובת הבוט: https://t.me/ttttran23bot")
    print(f"✅ הבוט פעיל! (Ctrl+C לעצירה)")
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
