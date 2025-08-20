from PIL import Image,ImageDraw,ImageFont
import math

def drawGauge(canvas : ImageDraw, x, y, size, value):
	canvas.chord([x, y, x+size, y+size], 180, 0, outline="YELLOW", fill = "BLACK", width = 1)
	yCenter = y+(size/2)
	xCenter = x+(size/2)
	
	angle_value = (value / 100) * 180
	
	lineX = size/2.2 * math.cos(math.radians(angle_value))
	lineY = size/2.2 * math.sin(math.radians(angle_value))
	canvas.line([xCenter, yCenter, xCenter-lineX, yCenter-lineY], fill="WHITE", width = 4)
	
	canvas.ellipse([xCenter-6, yCenter-6, xCenter+6, yCenter+6], fill="GREEN")
